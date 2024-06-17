"""
Nanotron Inference Script

Usage:
```
export CUDA_DEVICE_MAX_CONNECTIONS=1 # important for some distributed operations
torchrun --nproc_per_node=4 run_generate.py ---ckpt-path checkpoints/test/4
```
"""
import argparse
import os
from pathlib import Path

import torch
from nanotron import distributed as dist
from nanotron import logging
from nanotron.config import (
    GenerationArgs,
    LoggingArgs,
    ParallelismArgs,
    get_config_from_file,
)
from nanotron.distributed import get_global_rank
from nanotron.generation.decode import (
    GenerationInput,
    GenerationInputs,
    TokenizerConfig,
    decode_text,
    decode_tokenized,
)
from nanotron.generation.sampler import BasicSampler, GreedySampler, SamplerType, TopKSampler, TopPSampler
from nanotron.logging import log_rank, set_ranks_logging_level
from nanotron.models import build_model
from nanotron.parallel import ParallelContext
from nanotron.parallel.parameters import sanity_check
from nanotron.parallel.pipeline_parallel.engine import (
    OneForwardOneBackwardPipelineEngine,
)
from nanotron.parallel.pipeline_parallel.tensor_pointer import TensorPointer
from nanotron.parallel.tensor_parallel.enum import TensorParallelLinearMode
from nanotron.random import (
    RandomStates,
    get_current_random_state,
    get_synced_random_state,
    set_random_seed,
)
from nanotron.serialize import load_weights
from nanotron.trainer import CONFIG_TO_MODEL_CLASS, mark_tied_parameters

try:
    from transformers import AutoTokenizer
except ImportError:
    AutoTokenizer = None

import lovely_tensors as lt
from nanotron.parallel.pipeline_parallel.context_manager import attach_pipeline_state_to_model
from nanotron.parallel.pipeline_parallel.state import PipelineEvalBatchState

lt.monkey_patch()

logger = logging.get_logger(__name__)


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt-path", type=Path, required=True, help="Checkpoint path")
    parser.add_argument("--dp", type=int, default=1)
    parser.add_argument("--pp", type=int, default=1)
    parser.add_argument("--tp", type=int, default=1)
    parser.add_argument("--max-new-tokens", type=int, default=128, help="Maximum number of new tokens to generate")
    return parser.parse_args()


def main():
    args = get_args()

    assert args.ckpt_path.exists(), f"Checkpoint path {args.ckpt_path} does not exist"

    config = get_config_from_file((args.ckpt_path / "config.yaml").as_posix())
    model_config = config.model.model_config
    tokenizer_path = config.tokenizer.tokenizer_name_or_path

    parallel_config = ParallelismArgs(
        dp=args.dp,
        pp=args.pp,
        tp=args.tp,
        pp_engine=OneForwardOneBackwardPipelineEngine(),
        tp_mode=TensorParallelLinearMode.ALL_REDUCE,
        tp_linear_async_communication=False,
    )

    # Initialise all process groups
    parallel_context = ParallelContext(
        data_parallel_size=parallel_config.dp,
        pipeline_parallel_size=parallel_config.pp,
        tensor_parallel_size=parallel_config.tp,
    )

    # Set log levels
    logging_config = LoggingArgs(
        log_level="info",
        log_level_replica="info",
    )

    # Set log levels
    set_ranks_logging_level(parallel_context=parallel_context, logging_config=logging_config)

    log_rank(f"model_config: {model_config}", logger=logger, level=logging.INFO, rank=0)
    log_rank(f"tokenizer_path: {tokenizer_path}", logger=logger, level=logging.INFO, rank=0)

    dtype = torch.bfloat16

    # Set random states
    set_random_seed(42)

    model_config_cls = model_config.__class__.__name__
    if model_config_cls not in CONFIG_TO_MODEL_CLASS:
        raise ValueError(
            f"Unsupported model config {model_config_cls}. Only {CONFIG_TO_MODEL_CLASS.keys()} are supported"
        )

    # Get synchronized random states
    if parallel_config.tp_mode is TensorParallelLinearMode.ALL_REDUCE:
        random_states = RandomStates(
            {"tp_synced": get_synced_random_state(random_state=get_current_random_state(), pg=parallel_context.tp_pg)}
        )
    else:
        # We don't need to sync across TP when using sequence parallel (REDUCE_SCATTER)
        random_states = RandomStates({})

    model = build_model(
        model_builder=lambda: CONFIG_TO_MODEL_CLASS[model_config_cls](
            config=model_config,
            parallel_context=parallel_context,
            parallel_config=parallel_config,
            random_states=random_states,
        ),
        dtype=dtype,
        parallel_context=parallel_context,
    )

    # Mark some parameters as tied
    # TODO @nouamane: this is only needed for training, can we just mark params as NanotronParameter instead?
    mark_tied_parameters(model=model, parallel_context=parallel_context, parallel_config=parallel_config)

    # Sanity check model
    sanity_check(root_module=model)

    # Load checkpoint
    checkpoint_path = args.ckpt_path
    log_rank(
        f"Loading checkpoint from {checkpoint_path}:",
        logger=logger,
        level=logging.INFO,
        rank=0,
    )
    load_weights(model=model, parallel_context=parallel_context, root_folder=checkpoint_path)

    model.eval()
    if AutoTokenizer is not None:
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        # tokenizer.pad_token_id = tokenizer.eos_token_id
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is not None:
                tokenizer.pad_token_id = tokenizer.eos_token_id
            elif getattr(model.config, "pad_token_id", None) is not None:
                tokenizer.pad_token_id = int(model.config.pad_token_id)
            elif getattr(model.config, "eos_token_id", None) is not None:
                tokenizer.pad_token_id = int(model.config.eos_token_id)
            else:
                tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        tokenizer.padding_side = "left"
        tokenizer.truncation_side = "left"  # TODO @nouamane: do we want this?
        dummy_inputs = [
            "The future of AI is",
            # "Passage: Daniel went back to the garden. Mary travelled to the kitchen. Sandra journeyed to the kitchen. Sandra went to the hallway. John went to the bedroom. Mary went back to the garden. Where is Mary?\nAnswer:",
            # "def fib(n)",
            # 'Here is an extract from a webpage: "Have you ever experienced heel pain after a heavy physical activity, or even right after a long period of standing? If you regard this as something usual and normal, then think again. Miscalled as heel pain, plantar fasciitis causes these frequent mild pains experienced in the soles of the feet. It is the inflammation and enlargement the plantar fascia tissue that is located in the heels of the feet, stretching to the base of the toes. This tissue is responsible for absorbing shock in the feet and for supporting the arches. It also plays a vital role in foot movements during walking and standing. Many factors such as excessive walking, standing, and running trigger heel pain and plantar fasciitis. A sudden increase in intensity of activities, increase in weight, and abrupt change of footwear also cause the swelling of the ligament. Non-supportive footwear lacking arch cushions and improper and worn out running or training can also lead to the problem. It is also most evident among those". Write an extensive and detailed course unit suitable for a textbook targeted at college students, related to the given extract, within the context of "Medicine". Do not just list concepts, but develop each one in detail before moving to the next, as we prioritize depth of understanding and comprehensive exploration of the subject matter over breadth. Focus on: - Rigor: Ensure in-depth coverage of the concepts/sections. - Engagement: Write with an academic, professional and engaging tone that captivates interest. - Application: Incorporate specific, practical examples, such as proofs in calculus or critical dates and figures in history. Do not include a title or an introduction, simply write the content without headlines and introductory phrases. Do not use images.',
            # "Advancements in technology will lead to",
            # "Tomorrow's world is shaped by",
        ]

        if os.environ.get("REFACTO", "0") == "1":
            refactor_decode_text(
                args,
                parallel_context,
                model,
                tokenizer,
                dummy_inputs,
                generation_config=GenerationArgs(sampler="greedy", use_cache=False),
            )
        # print("==================================================")
        else:
            outputs = decode_text(
                input_iter=(GenerationInput(text=text) for text in dummy_inputs),
                tokenizer=tokenizer,
                # TODO @thomasw21: From ModelWithLoss extract the model.
                model=model.model,
                parallel_context=parallel_context,
                max_new_tokens=args.max_new_tokens,
                max_micro_batch_size=2,
                generation_config=GenerationArgs(sampler="greedy", use_cache=False),
                tokenizer_config=TokenizerConfig(max_input_length=None),
                is_bench=os.environ.get("USE_BENCH", "0") == "1",
            )

            for output in outputs:
                input_ids = output.input_ids
                generated_ids = output.generation_ids
                if isinstance(input_ids, TensorPointer):
                    assert isinstance(generated_ids, TensorPointer)
                    continue
                assert isinstance(generated_ids, torch.Tensor)

                log_rank(
                    f"input: {tokenizer.decode(input_ids, clean_up_tokenization_spaces=False)[:1000]}",
                    logger=logger,
                    level=logging.INFO,
                    rank=0,
                )

                log_rank(
                    f"generation: {tokenizer.decode(generated_ids[len(input_ids) :], clean_up_tokenization_spaces=False)}",
                    logger=logger,
                    level=logging.INFO,
                    rank=0,
                )

                log_rank(
                    "--------------------------------------------------",
                    logger=logger,
                    level=logging.INFO,
                    rank=0,
                )
    else:
        outputs = decode_tokenized(
            input_ids=torch.zeros(1, 1).to(dtype=torch.int64, device="cuda"),
            input_mask=torch.ones(1, 1).to(dtype=torch.bool, device="cuda"),
            model=model.model,
            parallel_context=parallel_context,
            generation_config=GenerationArgs(sampler="greedy", use_cache=True),
            max_micro_batch_size=1,
            max_new_tokens=12,
            returns_logits=False,
        )
        for output in outputs:
            input_ids = output.input_ids
            generated_ids = output.generation_ids
            if isinstance(input_ids, TensorPointer):
                assert isinstance(generated_ids, TensorPointer)
                continue
            assert isinstance(generated_ids, torch.Tensor)
            log_rank(
                f"generation: {generated_ids[len(input_ids) :]}",
                logger=logger,
                level=logging.INFO,
                rank=0,
            )

            log_rank(
                "--------------------------------------------------",
                logger=logger,
                level=logging.INFO,
                rank=0,
            )

    dist.barrier()


def run_one_inference_step(model, batch, parallel_context, device):
    if dist.get_world_size(group=parallel_context.pp_pg) == 1:
        return model.model(batch.input_ids, batch.input_masks)

    pipeline_state = PipelineEvalBatchState()
    with attach_pipeline_state_to_model(model=model, pipeline_state=pipeline_state):

        batch_size = batch.input_ids.shape[0]
        seq_len = batch.input_ids.shape[1]

        # Preallocate memory for output logits.
        logits = None
        if parallel_context.is_pipeline_last_stage:
            logits = torch.empty((seq_len, batch_size, model.config.vocab_size), dtype=torch.float32, device=device)

        batch2use = GenerationInputs(
            input_ids=batch.input_ids
            if parallel_context.is_pipeline_first_stage
            else TensorPointer(group_rank=parallel_context.pipeline_parallel_prev_rank),
            input_masks=batch.input_masks
            if parallel_context.is_pipeline_first_stage
            else TensorPointer(group_rank=parallel_context.pipeline_parallel_prev_rank),
        )

        output_tensor = model.model(batch2use.input_ids, batch2use.input_masks)

        # TODO: Check if we need to send only 2

        nb_send = len(pipeline_state.microbatches_activations_to_send)
        assert nb_send <= 2
        for _ in range(nb_send):
            pipeline_state.run_communication()

        # Copy logits.
        if parallel_context.is_pipeline_last_stage:
            logits = output_tensor

        # Wait for all the communication to complete.
        dist.barrier(group=parallel_context.world_pg)

        return logits


def refactor_decode_text(args, parallel_context, model, tokenizer, dummy_inputs, generation_config):
    device = torch.cuda.current_device()

    if generation_config:
        if isinstance(generation_config.sampler, str):
            sampler_type = SamplerType(generation_config.sampler.upper())
        else:
            sampler_type = generation_config.sampler
    else:
        sampler_type = SamplerType.GREEDY

    # TODO: add batch inference
    # TODO: add decoded_tokenize
    # TODO: add benchmark

    tokenized_prompts = tokenizer(
        dummy_inputs,
        return_tensors="pt",
        return_attention_mask=True,
        padding=True,
    )

    tokenized_prompts["input_ids"] = tokenized_prompts["input_ids"].to(device)
    tokenized_prompts["attention_mask"] = tokenized_prompts["attention_mask"].to(dtype=torch.bool, device=device)

    for _ in range(args.max_new_tokens):
        batch_prompts = GenerationInputs(
            input_ids=tokenized_prompts["input_ids"],
            input_masks=tokenized_prompts["attention_mask"],
        )

        logits = run_one_inference_step(model, batch_prompts, parallel_context, device)

        # Sample new token
        if parallel_context.is_pipeline_last_stage:
            assert logits is not None and isinstance(logits, torch.Tensor)

            # TODO(fmom): dont transpose if it is mamba. Add if "logits_are_batch_first" flag
            logits = logits.transpose(0, 1)

            # TODO: Use cache
            if sampler_type == SamplerType.GREEDY:
                sampler = GreedySampler(pg=parallel_context.tp_pg)
            elif sampler_type == SamplerType.TOP_K:
                sampler = TopKSampler(pg=parallel_context.tp_pg)
            elif sampler_type == SamplerType.TOP_P:
                sampler = TopPSampler(pg=parallel_context.tp_pg)
            elif sampler_type == SamplerType.BASIC:
                sampler = BasicSampler(pg=parallel_context.tp_pg)
            else:
                raise NotImplementedError(f"Sampler type {sampler_type} is not implemented")

            next_token = sampler(sharded_logits=logits[:, -1])

            tokenized_prompts["input_ids"] = torch.cat([tokenized_prompts["input_ids"], next_token], dim=-1)
            tokenized_prompts["attention_mask"] = torch.cat(
                [
                    tokenized_prompts["attention_mask"],
                    torch.ones((tokenized_prompts["attention_mask"].shape[0], 1), dtype=torch.int64, device=device),
                ],
                dim=-1,
            )
        else:
            # Extend the tokenized prompts to receive the new token
            tokenized_prompts["input_ids"] = torch.zeros(
                (tokenized_prompts["input_ids"].shape[0], tokenized_prompts["input_ids"].shape[1] + 1),
                dtype=torch.int64,
                device=device,
            )
            tokenized_prompts["attention_mask"] = torch.zeros(
                (tokenized_prompts["attention_mask"].shape[0], tokenized_prompts["attention_mask"].shape[1] + 1),
                dtype=torch.int64,
                device=device,
            )

        # Broadcast the new token to all the pipeline stages
        dist.broadcast(
            tokenized_prompts["input_ids"],
            src=get_global_rank(group=parallel_context.pp_pg, group_rank=parallel_context.pipeline_parallel_last_rank),
            group=parallel_context.pp_pg,
        )
        dist.broadcast(
            tokenized_prompts["attention_mask"],
            src=get_global_rank(group=parallel_context.pp_pg, group_rank=parallel_context.pipeline_parallel_last_rank),
            group=parallel_context.pp_pg,
        )

    if dist.get_rank() == 0:
        for i, prompt in enumerate(dummy_inputs):
            tokenized_outputs = tokenized_prompts["input_ids"][
                i, tokenized_prompts["input_ids"].shape[1] - args.max_new_tokens :
            ]
            outputs = tokenizer.decode(tokenized_outputs, clean_up_tokenization_spaces=False)

            # Convert with log_rank
            log_rank(f"Input: {prompt}", logger=logger, level=logging.INFO, rank=0)
            log_rank(f"Output: {outputs}", logger=logger, level=logging.INFO, rank=0)


if __name__ == "__main__":
    main()
