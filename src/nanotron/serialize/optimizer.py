import json
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Tuple

import torch
from torch import nn
from tqdm import tqdm

from nanotron import distributed as dist
from nanotron import logging, optim
from nanotron.constants import OPTIMIZER_CONFIG_FILE_NAME
from nanotron.logging import log_rank
from nanotron.optim.zero import (
    ZeroDistributedOptimizer,
    extract_parallel_ranks_from_shard_path,
    find_optim_index_from_param_name,
    get_sliced_tensor,
    merge_dp_shard_in_zero1_optimizer,
)
from nanotron.parallel import ParallelContext
from nanotron.parallel.parameters import NanotronParameter
from nanotron.serialize.metadata import TensorMetadata
from nanotron.serialize.utils import ObjectType, merge_and_shard_tp_tensors

logger = logging.get_logger(__name__)


def get_optimizer_filename(
    tp_topology: Tuple[int, int],
    pp_topology: Tuple[int, int],
    dp_topology: Optional[Tuple[int, int]] = None,
    exp_topology: Optional[Tuple[int, int]] = None,
    is_zero: Optional[bool] = None,
):
    """
    tp_topology: Tuple[int, int] = (rank, size)
    pp_topology: Tuple[int, int] = (rank, size)
    dp_topology: Tuple[int, int] = (rank, size)

    NOTE: sometimes we get the checkpoint from a different topology (not the current parallel_context)
    """
    assert exp_topology is not None, "exp_topology is required"
    assert is_zero is not None, "is_zero is required"
    pp_rank, pp_size = pp_topology
    tp_rank, tp_size = tp_topology
    exp_rank, exp_size = exp_topology

    if is_zero is True:
        dp_rank, dp_size = dp_topology
        return f"{ObjectType.OPTIMIZER.value}_pp-{pp_rank}-of-{pp_size}_dp-{dp_rank}-of-{dp_size}_tp-{tp_rank}-of-{tp_size}_exp-{exp_rank}-of-{exp_size}.pt"
    else:
        return f"{ObjectType.OPTIMIZER.value}_pp-{pp_rank}-of-{pp_size}_tp-{tp_rank}-of-{tp_size}_exp-{exp_rank}-of-{exp_size}.pt"


def lr_scheduler_filename(parallel_context: ParallelContext, is_zero: bool):
    if is_zero is True:
        return f"{ObjectType.LR_SCHEDULER.value}_pp-{dist.get_rank(parallel_context.pp_pg)}-of-{parallel_context.pp_pg.size()}_dp-{dist.get_rank(parallel_context.dp_pg)}-of-{parallel_context.dp_pg.size()}_tp-{dist.get_rank(parallel_context.tp_pg)}-of-{parallel_context.tp_pg.size()}_exp-{dist.get_rank(parallel_context.expert_pg)}-of-{parallel_context.expert_parallel_size}.pt"
    else:
        return f"{ObjectType.LR_SCHEDULER.value}_pp-{dist.get_rank(parallel_context.pp_pg)}-of-{parallel_context.pp_pg.size()}_tp-{dist.get_rank(parallel_context.tp_pg)}-of-{parallel_context.tp_pg.size()}_exp-{dist.get_rank(parallel_context.expert_pg)}-of-{parallel_context.expert_parallel_size}.pt"


def save_optimizer(
    optimizer: optim.BaseOptimizer,
    parallel_context: ParallelContext,
    root_folder: Path,
):
    """Saves optimizer states
    - If Zero-0 is used, optimizer states are replicated across all DPs. Only DP-0 saves the states
    - If Zero-1 is used, optimizer states are sharded across all DPs. Each DP saves its own states
    """
    if (not optimizer.inherit_from(optim.ZeroDistributedOptimizer)) and dist.get_rank(parallel_context.dp_pg) > 0:
        # this is Zero-0, so only DP-0 saves the optimizer states
        return

    # TODO: Figure out if I need to save param groups. Right now I'm assuming no as we only store what's trainable
    # TODO: We can probably "rotate" so that every process stores something (maybe doesn't matter if we're I/O bound)
    root_folder = root_folder / "optimizer"
    root_folder.mkdir(exist_ok=True, parents=True)

    if dist.get_rank(parallel_context.world_pg) == 0:
        with open(root_folder / OPTIMIZER_CONFIG_FILE_NAME, "w") as fo:
            tp_size = parallel_context.tp_pg.size()
            pp_size = parallel_context.pp_pg.size()
            dp_size = parallel_context.dp_pg.size()
            expert_parallel_size = parallel_context.expert_parallel_size

            config = {
                "type": str(optimizer.__class__.__name__),
                "parallelism": {
                    "tp_size": str(tp_size),
                    "dp_size": str(dp_size),
                    "pp_size": str(pp_size),
                    "expert_parallel_size": str(expert_parallel_size),
                },
                "configs": {},
            }

            if isinstance(optimizer, ZeroDistributedOptimizer):
                # NOTE: in order to serialize, we must save all keys and values as strings
                def convert_to_string(input_item):
                    if isinstance(input_item, dict):
                        return {str(key): convert_to_string(value) for key, value in input_item.items()}
                    elif isinstance(input_item, list):
                        return [convert_to_string(element) for element in input_item]
                    elif isinstance(input_item, tuple):
                        return tuple(convert_to_string(element) for element in input_item)
                    else:
                        return str(input_item)

                # NOTE: if it's a ZeRO-1 optimzier, then we save how the parameters are sharded
                # across data parallel dimension, so that we can reconstruct the optimizer states
                assert optimizer.param_name_to_dp_rank_offsets is not None, "param_name_to_dp_rank_offsets is required"
                config["configs"]["param_name_to_dp_rank_offsets"] = convert_to_string(
                    optimizer.param_name_to_dp_rank_offsets
                )
                # NOTE: since tp sharded params are flattened, so we need to save the original param shapes
                # so that we can recontruct the original shapes => reconstruct the unsharded params in tensor parallel dimension
                config["configs"]["orig_param_shapes"] = convert_to_string(optimizer._orig_param_shapes)

            json.dump(config, fo)

    # We dump the optimizer state using `torch.save`
    torch.save(
        optimizer.state_dict(),
        root_folder
        / get_optimizer_filename(
            tp_topology=(dist.get_rank(parallel_context.tp_pg), parallel_context.tp_pg.size()),
            pp_topology=(dist.get_rank(parallel_context.pp_pg), parallel_context.pp_pg.size()),
            dp_topology=(dist.get_rank(parallel_context.dp_pg), parallel_context.dp_pg.size()),
            exp_topology=(dist.get_rank(parallel_context.expert_pg), parallel_context.expert_parallel_size),
            is_zero=optimizer.inherit_from(optim.ZeroDistributedOptimizer),
        ),
    )


def save_lr_scheduler(
    lr_scheduler,
    is_zero,
    parallel_context: ParallelContext,
    root_folder: Path,
):
    """Saves lr scheduler states"""
    if not is_zero and dist.get_rank(parallel_context.dp_pg) > 0:
        # this is Zero-0, so only DP-0 saves the optimizer states
        return

    root_folder = root_folder / "lr_scheduler"
    root_folder.mkdir(exist_ok=True, parents=True)

    # We dump the optimizer state using `torch.save`
    torch.save(
        lr_scheduler.state_dict(),
        root_folder / lr_scheduler_filename(parallel_context, is_zero),
    )


# Helper functions to move optimizer states
@torch.no_grad()
def state_dict_to_device(state_dict: Dict, device: str) -> Dict:
    assert (
        state_dict["state"][0]["exp_avg"].device.type == "cpu"
    ), "Optimizer states should be on CPU to avoid extra memory usage when loading from checkpoint"
    torch.cuda.empty_cache()

    for _, optim_state in sorted(state_dict["state"].items(), key=lambda x: x[0]):
        for name, tensor in optim_state.items():
            optim_state[name] = tensor.to(device)

    for name, tensor in state_dict["gradient_accumulator"].items():
        state_dict["gradient_accumulator"][name] = tensor.to(device)

    assert (
        state_dict["state"][0]["exp_avg"].device.type == "cuda"
    ), "Optimizer states should be on GPU because model is on GPU"
    torch.cuda.empty_cache()


@torch.no_grad()
def load_optimizer(
    optimizer: optim.BaseOptimizer,
    parallel_context: ParallelContext,
    root_folder: Path,
    map_location: Optional[str] = None,
    param_shard_metadata: Tuple[Tuple[int, int], TensorMetadata] = None,  # (pp_rank, tp_rank) -> TensorMetadata
    model: Optional[nn.Module] = None,
):
    root_folder = root_folder / "optimizer"
    ckp_optimizer_config_path = root_folder / OPTIMIZER_CONFIG_FILE_NAME
    with open(ckp_optimizer_config_path, "r") as file:
        ckp_optimizer_config = json.load(file)

    ckp_pp_size = ckp_optimizer_config["parallelism"]["pp_size"]
    ckp_tp_size = ckp_optimizer_config["parallelism"]["tp_size"]
    ckp_dp_size = ckp_optimizer_config["parallelism"]["dp_size"]
    ckpt_expert_parallel_size = ckp_optimizer_config["parallelism"]["expert_parallel_size"]

    # NOTE: tensor parallel, and pipeline paralell's optimizer state-agnotic loading
    if int(ckp_tp_size) != int(parallel_context.tp_pg.size()) or int(ckp_pp_size) != int(
        parallel_context.pp_pg.size()
    ):
        if int(ckp_pp_size) != int(parallel_context.pp_pg.size()):
            warnings.warn(
                "You are resuming in a different PP size, so optimizer states need to be checked. Feel free to open a PR if you work on this!"
            )
        assert (
            param_shard_metadata is not None
        ), f"You have to pass how the original parameters are sharded in order to resume in a different tensor parallel size, ckp_tp_size: {ckp_tp_size}, current tp_size: {parallel_context.tp_pg.size()}"
        assert (
            model is not None
        ), "You have to pass the model in order to adjust the optimizer states according to how the current parameters are sharded"

        def get_checkpoint_state_metadata(param_name: str, pp_rank: int, tp_rank: int) -> TensorMetadata:
            return param_shard_metadata[param_name.replace("module.", "")][(str(pp_rank), str(tp_rank))]

        ckp_optim_type = ckp_optimizer_config["type"]

        if ckp_optim_type == ZeroDistributedOptimizer.__name__:
            # NOTE: if the checkpoint is from a Zero-1 optimizer, then we need to merge the shards
            # across data parallel dimension, before merging the shards across tensor parallel dimension
            shard_paths = list(
                root_folder.glob(
                    f"{ObjectType.OPTIMIZER.value}_pp-*-of-{ckp_pp_size}_dp-*-of-{ckp_dp_size}_tp-*-of-{ckp_tp_size}_exp-*-of-{ckpt_expert_parallel_size}.pt"
                )
            )
            ckp_sharded_optim_states = merge_dp_shard_in_zero1_optimizer(
                model, ckp_optimizer_config, shard_paths, parallel_context, map_location
            )
        else:
            # NOTE: if the checkpoint is from a Zero-0 optimizer, then we don't need to merge the shards
            # across data parallel dimension, just directly load the checkpoints
            shard_paths = list(
                root_folder.glob(
                    f"{ObjectType.OPTIMIZER.value}_pp-*-of-{ckp_pp_size}_tp-*-of-{ckp_tp_size}.pt"
                )  # WARN: wildcard here after tp can hold `0-of-1_exp-0`
            )

            ckp_sharded_optim_states = {}
            for shard_path in shard_paths:
                pp_rank, tp_rank = extract_parallel_ranks_from_shard_path(shard_path, is_zero1=False)
                ckp_sharded_optim_states[(pp_rank, tp_rank)] = torch.load(
                    shard_path, map_location=map_location
                )  # load all optim states in mem

        model_state_dict = model.state_dict()
        new_optim_state_dict = optimizer.state_dict()
        new_optim_state_dict["state"] = defaultdict(dict)
        # TODO: this does not handle the edge case of different pipeline parallel optimizer state shards saving different state keys
        OPTIMIZER_STATE_NAMES = sorted(ckp_sharded_optim_states[(0, 0)]["state"][0].keys() - ["step"])
        OPTIMIZER_STATE_DTYPE = ckp_sharded_optim_states[(0, 0)]["state"][0][OPTIMIZER_STATE_NAMES[0]].dtype
        # NOTE: because we can only resume training with the same optimizer type
        # (0, 0) = (pp_rank, tp_rank)
        # NOTE: also we don't merge "step" because it's just a scalar
        param_names = list(model_state_dict.keys())
        new_optim_state_param_names = {}
        # NOTE: iterates through all model parameters in the local pipeline parallel rank (hence, might not be the full model).
        # Since model parameters and optimizer states are aligned, loads only the optimizer states for these parameters from the checkpoint shards.
        for param_index, param_name in tqdm(
            enumerate(param_names),
            disable=dist.get_rank(parallel_context.world_pg) != 0,
            desc="Topology-agnostic optimizer loading",
        ):
            try:
                param = model.get_parameter(param_name)
            except AttributeError:
                param = None

            if not isinstance(param, NanotronParameter):
                raise NotImplementedError("Parameters are required to be NanotronParameter")

            # NOTE: for tied parameters, the metadata is stored using the parameter name,
            # while the data is stored using the name of the main tied parameter,
            # which may be different (e.g. `model.token_position_embeddings.pp_block.token_embedding.weight`
            # for `model.lm_head.pp_block.weight`).
            base_name = param.get_tied_info().name if param.is_tied else param_name
            if param_name != base_name:
                # NOTE: skip tied parameter if main tied parameter has already been loaded
                # (not always the case if pipeline parallel)
                if base_name in new_optim_state_param_names.values():
                    continue
            new_optim_state_param_names[param_index] = base_name

            if param.is_sharded:
                # NOTE: optimizer states's shape is equal to the parameter's shape
                # NOTE: sometimes an unsharded parameter's shape differ
                # from an unsharded optimizer state's shape
                new_shard_metadata = param.get_sharded_info()
                new_unshared_shape = new_shard_metadata.unsharded_shape
                # NOTE: restore each state tensor (e.g. exg_avg) by iterating through
                # the optimizer state shards saved using the previous topology
                for state_key in OPTIMIZER_STATE_NAMES:
                    # TODO(xrsrke): free the memory of the shards that isn't
                    # corresponding to the current rank
                    # TODO: maybe better to allocate memory for all states at once
                    buffer = torch.zeros_like(param, device=map_location, dtype=OPTIMIZER_STATE_DTYPE)
                    unsharded_buffer = torch.empty(
                        new_unshared_shape, device=map_location, dtype=OPTIMIZER_STATE_DTYPE
                    )

                    for (pp_rank, tp_rank), ckp_optim_state in ckp_sharded_optim_states.items():
                        old_optim_state_index = find_optim_index_from_param_name(
                            base_name, ckp_sharded_optim_states, is_zero1=False, pp_rank=pp_rank
                        )
                        if old_optim_state_index is None:
                            continue  # NOTE: param is not in this pp shard
                        ckp_shard_data = ckp_optim_state["state"][old_optim_state_index][state_key]
                        # NOTE: the metadata for the main parameter of a tied parameter might be in a
                        # different pipeline parallel shard.
                        if param.is_tied:
                            metadata_pp_rank = next(
                                iter(param_shard_metadata[param_name.replace("module.", "")].keys())
                            )[0]
                        else:
                            metadata_pp_rank = pp_rank
                        ckp_shard_metadata = get_checkpoint_state_metadata(param_name, metadata_pp_rank, tp_rank)

                        # NOTE: if the checkpoint is from a Zero-1 optimizer,
                        # so it's flattened, so we need to reshape it
                        if ckp_optim_type == ZeroDistributedOptimizer.__name__:
                            # NOTE: this is the original shape of the parameter before being flattened
                            orig_shape = ckp_optimizer_config["configs"]["orig_param_shapes"][param_name]
                            orig_shape = [int(dim) for dim in orig_shape]
                            ckp_shard_data = ckp_shard_data.view(orig_shape)

                        new_optim_state_dict["state"][param_index][state_key] = merge_and_shard_tp_tensors(
                            buffer,
                            unsharded_buffer,
                            [
                                (ckp_shard_data, ckp_shard_metadata.local_global_slices_pairs),
                            ],
                            new_shard_metadata,
                        )
            else:
                # Handle non-sharded params (e.g. layernorm)
                for (pp_rank, tp_rank), ckp_optim_state in ckp_sharded_optim_states.items():
                    old_optim_state_index = find_optim_index_from_param_name(
                        base_name, ckp_sharded_optim_states, is_zero1=False, pp_rank=pp_rank
                    )
                    if old_optim_state_index is None:
                        continue  # Param not in this PP shard

                    # For non-sharded params, just copy over the state directly
                    for state_key in OPTIMIZER_STATE_NAMES:
                        new_optim_state_dict["state"][param_index][state_key] = ckp_optim_state["state"][
                            old_optim_state_index
                        ][state_key]

            if ckp_optim_type == ZeroDistributedOptimizer.__name__:
                # NOTE: flatten the optimizer states
                new_optim_state_dict["state"][param_index][state_key] = new_optim_state_dict["state"][param_index][
                    state_key
                ].flatten()

            # NOTE: a bit awkward, but while we're already reading this (pp,tp) shard for whatever state_key,
            # try to get the step value as well.
            step = ckp_optim_state["state"][old_optim_state_index].get("step")
            if step is not None:
                new_optim_state_dict["state"][param_index]["step"] = step

            # NOTE: we throw away ckp_optim_state['gradient_accumulator'] which has fp32 grads

        new_optim_state_dict["names"] = new_optim_state_param_names
        state_dict = new_optim_state_dict
    else:
        # NOTE: since here we only load the optimizer states,
        # then we shard it according to the current data parallel dimension
        # TODO @thomasw21: Load optimizer type and check that it's compatible otherwise we might be be loading something else completely
        state_dict = torch.load(
            root_folder
            / get_optimizer_filename(
                tp_topology=(dist.get_rank(parallel_context.tp_pg), parallel_context.tp_pg.size()),
                pp_topology=(dist.get_rank(parallel_context.pp_pg), parallel_context.pp_pg.size()),
                # NOTE(xrsrke): suppose we initially have dp world size of 4,
                # then we change to dp world size of 8, then we need to load the optimizer states
                # now we do a round-robin mapping of the optimizer states to the new dp world size
                # dp=8's ranks: [0, 1, 2, 3, 4, 5, 6, 7]
                # maps to: [0, 1, 2, 3, 0, 1, 2, 3]
                dp_topology=(int(dist.get_rank(parallel_context.pp_pg)) // int(ckp_dp_size), ckp_dp_size),
                exp_topology=(dist.get_rank(parallel_context.expert_pg), parallel_context.expert_parallel_size),
                is_zero=optimizer.inherit_from(optim.ZeroDistributedOptimizer),
            ),
            map_location=map_location,
        )

    def create_merged_optim_states(param_shapes, map_location):
        merged_states = {}
        for name, p_shape in param_shapes.items():
            p_shape = tuple(int(x) for x in p_shape)
            merged_states[name] = {
                "exp_avg": torch.zeros(p_shape).view(-1).to(map_location),
                "exp_avg_sq": torch.zeros(p_shape).view(-1).to(map_location),
            }
        return merged_states

    def create_merged_gradients(param_shapes, map_location):
        merged_grads = {}
        for name, p_shape in param_shapes.items():
            p_shape = tuple(int(x) for x in p_shape)
            merged_grads[name] = torch.zeros(p_shape).view(-1).to(map_location)
        return merged_grads

    def load_sharded_states(shard_paths, map_location, load_type="state"):
        sharded_states = {}
        for shard_path in shard_paths:
            pp_rank, dp_rank, tp_rank = extract_parallel_ranks_from_shard_path(shard_path, is_zero1=True)
            checkpoint = torch.load(shard_path, map_location=map_location)
            sharded_states[(tp_rank, dp_rank)] = checkpoint[load_type]
        return sharded_states

    def get_key_by_value(d, target_value):
        return next((key for key, value in d.items() if value == target_value), None)

    def apply_offsets(merged_tensor, sharded_states, param_name, offsets, tp_rank, state_keys=None):
        if state_keys:
            for key in state_keys:
                p_idx = get_key_by_value(state_dict["names"], param_name)
                merged_tensor[param_name][key][int(offsets[0]) : int(offsets[1])] = sharded_states[
                    (int(tp_rank), int(dp_rank))
                ][p_idx][key]
        else:
            merged_tensor[param_name][int(offsets[0]) : int(offsets[1])] = sharded_states[
                (int(tp_rank), int(dp_rank))
            ][param_name]

    if isinstance(optimizer, ZeroDistributedOptimizer):
        shard_paths = list(
            root_folder.glob(
                f"{ObjectType.OPTIMIZER.value}_pp-*-of-{ckp_pp_size}_dp-*-of-{ckp_dp_size}_tp-*-of-{ckp_tp_size}_exp-*-of-{ckpt_expert_parallel_size}.pt"
            )
        )

        if int(ckp_dp_size) != parallel_context.dp_pg.size():
            log_rank(
                f"[Optimizer Loading] Detect new data parallelism topology in ZeRO-1, resharding optimizer states and gradient accumulator's states",  # noqa
                logger=logger,
                level=logging.INFO,
                rank=0,
            )

            current_dp_rank = dist.get_rank(parallel_context.dp_pg)
            tp_rank = dist.get_rank(parallel_context.tp_pg)
            OPTIMIZER_STATE_NAMES = state_dict["state"][0].keys() - ["step"]
            param_shapes = ckp_optimizer_config["configs"]["orig_param_shapes"]

            # Handle optimizer states
            ckp_sharded_optim_states = load_sharded_states(shard_paths, map_location, "state")
            merged_optim_states = create_merged_optim_states(param_shapes, map_location)

            for p_name, offsets in ckp_optimizer_config["configs"]["param_name_to_dp_rank_offsets"].items():
                for dp_rank, offset in offsets.items():
                    apply_offsets(
                        merged_optim_states, ckp_sharded_optim_states, p_name, offset, tp_rank, OPTIMIZER_STATE_NAMES
                    )

            # Update state dict with new sliced tensors
            for param_index in state_dict["state"]:
                param_name = [name for idx, name in state_dict["names"].items() if idx == param_index][0]
                for state_name in OPTIMIZER_STATE_NAMES:
                    current_offsets = optimizer.param_name_to_dp_rank_offsets[param_name][current_dp_rank]
                    sliced_tensor = get_sliced_tensor(
                        param=merged_optim_states[param_name][state_name],
                        start_offset=current_offsets[0],
                        end_offset=current_offsets[1],
                    )
                    assert sliced_tensor.numel() > 0
                    state_dict["state"][param_index][state_name] = sliced_tensor

            # Handle gradient accumulator if DP size changed
            assert int(ckp_tp_size) == parallel_context.tp_pg.size(), "Don't support changing TP size for ZeRO-1"

            ckp_sharded_grad_accum = load_sharded_states(shard_paths, map_location, "gradient_accumulator")
            merged_grad_accumulator = create_merged_gradients(param_shapes, map_location)

            for p_name, offsets in ckp_optimizer_config["configs"]["param_name_to_dp_rank_offsets"].items():
                for dp_rank, offset in offsets.items():
                    apply_offsets(merged_grad_accumulator, ckp_sharded_grad_accum, p_name, offset, tp_rank)

            # Update gradient accumulator with new slices
            for p_name in state_dict["gradient_accumulator"].keys():
                new_offset = optimizer.param_name_to_dp_rank_offsets[p_name][int(dp_rank)]
                assert state_dict["gradient_accumulator"][p_name].device == merged_grad_accumulator[p_name].device
                state_dict["gradient_accumulator"][p_name] = merged_grad_accumulator[p_name][
                    int(new_offset[0]) : int(new_offset[1])
                ]

        optimizer.load_state_dict(state_dict, map_location=map_location)


def load_lr_scheduler(
    lr_scheduler,
    is_zero,
    parallel_context: ParallelContext,
    root_folder: Path,
):
    root_folder = root_folder / "lr_scheduler"

    state_dict = torch.load(root_folder / lr_scheduler_filename(parallel_context, is_zero))
    lr_scheduler.load_state_dict(state_dict)
    lr_scheduler._initial_step()  # NOTE: this is required to set the initial learning rate
