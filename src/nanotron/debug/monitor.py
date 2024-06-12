from typing import Dict, List, Optional, Tuple, Union

import torch
import torch.distributed as dist
from nanotron.constants import DEBUG_PATH
from nanotron.models.base import NanotronModel
from nanotron.parallel import ParallelContext
from torch import nn
from torch.distributed import ReduceOp


def track_weight_and_grad_stats(name: str, module: nn.Module, parallel_context: ParallelContext):
    def compute_stats(name, tensors):
        tensors = {"tensor": tensors} if not isinstance(tensors, dict) else tensors
        stats = {}

        for key, tensor in tensors.items():
            if tensor.dtype == torch.long or tensor.dtype == torch.int or tensor.dtype == torch.bool:
                continue

            if tensor is None:
                assert 1 == 1

            stats[key] = {}
            stats[key] = {
                "mean": tensor.cpu().mean().item(),
                # "std": tensor.std().item(),
                # "var": tensor.var().item(),
                # "norm": tensor.norm().item(),
                # "min": tensor.min().item(),
                # "max": tensor.max().item(),
                "amax": tensor.cpu().abs().max().item(),
            }

            # NOTE: now all reduce mean this across tp ranks
            tp_group = parallel_context.tp_pg
            for metric_name, metric_value in stats[key].items():
                stats[key][metric_name] = torch.tensor(metric_value, device=tensor.device, dtype=tensor.dtype)
                dist.all_reduce(stats[key][metric_name], op=ReduceOp.MAX, group=tp_group)

            # tp_rank = dist.get_rank(group=tp_group)
            # stats[key][f"data:tp_{tp_rank}"] = tensor.detach().cpu().tolist()

        return stats[list(stats.keys())[0]] if len(stats) == 1 else stats

    logs: Dict[str, Dict[str, float]] = {}

    if name not in logs:
        logs[name] = {}

    def _save_output_stats(module: nn.Module, input: torch.Tensor, output: torch.Tensor):
        param_names = [name for name, _ in module.named_parameters()]
        for param_name in param_names:
            if hasattr(module, param_name):
                param = getattr(module, param_name)
                stats = compute_stats(name, param.data)
                if stats is not None:
                    logs[name][param_name] = stats

        inputs = input if isinstance(input, tuple) else (input,)
        outputs = output if isinstance(output, tuple) else (output,)

        if len(inputs) > 1:
            for i, inp in enumerate(inputs):
                if inp.dtype == torch.long:
                    # NOTE: this is input ids in transformers
                    continue
                stats = compute_stats(name, inp)
                if stats is not None:
                    logs[name][f"input:{i}"] = stats
        elif len(inputs) == 1:
            stats = compute_stats(name, inputs[0])
            if stats is not None:
                logs[name]["input"] = stats

        dp_rank = dist.get_rank(group=parallel_context.dp_pg)
        tp_rank = dist.get_rank(group=parallel_context.tp_pg)
        pp_rank = dist.get_rank(group=parallel_context.pp_pg)

        def save(name, tensor):
            import os

            # from nanotron.constants import
            # DIR = "./debug/nn_states_with_bs_1/acts/"

            os.makedirs(DEBUG_PATH, exist_ok=True)

            if dp_rank == 0:
                torch.save(
                    tensor, f"{DEBUG_PATH}/{name}_dp_rank_{dp_rank}_and_pp_rank_{pp_rank}_and_tp_rank_{tp_rank}.pt"
                )

        if len(outputs) > 1:
            for i, out in enumerate(outputs):
                stats = compute_stats(name, out)
                save(name, out)
                if stats is not None:
                    logs[name][f"output:{i}"] = stats
        elif len(outputs) == 1:
            stats = compute_stats(name, outputs[0])
            save(name, outputs[0])
            if stats is not None:
                logs[name]["output"] = stats

    def _save_grad_stats(module: nn.Linear, grad_input, grad_output: torch.Tensor):
        if isinstance(grad_output, tuple):
            for i, grad in enumerate(grad_output):
                if grad is None:
                    continue

                stats = compute_stats(name, grad)
                if stats is not None:
                    logs[name][f"grad_output:{i}"] = stats
        else:
            stats = compute_stats(name, grad_output)
            if stats is not None:
                logs[name]["grad_output"] = stats

        if isinstance(grad_input, tuple):
            for i, grad in enumerate(grad_input):
                if grad is not None:
                    stats = compute_stats(name, grad)
                    if stats is not None:
                        logs[name][f"grad_input:{i}"] = stats
        else:
            if grad_input is not None:
                stats = compute_stats(name, grad_input)
                if stats is not None:
                    logs[name]["grad_input"] = stats

    handles = []
    handles.append(module.register_forward_hook(_save_output_stats))
    handles.append(module.register_backward_hook(_save_grad_stats))
    return logs, handles


def monitor_nanotron_model(model: NanotronModel, parallel_context: Optional[ParallelContext] = None):
    assert parallel_context is not None

    def get_leaf_modules(module: nn.Module) -> List[Tuple[str, nn.Module]]:
        """
        Return all the leaf modules (modules without any child modules) in a PyTorch module.
        """
        leaf_modules = []
        for n, m in module.named_modules():
            if not list(m.children()):
                leaf_modules.append((n, m))
        return leaf_modules

    logs = {}
    handles = []
    # leaf_modules = get_leaf_modules(model)
    leaf_modules = [(name, module) for name, module in model.named_modules()]

    for name, module in leaf_modules:
        module_logs, module_handles = track_weight_and_grad_stats(name, module, parallel_context)
        logs.update(module_logs)
        handles.extend(module_handles)

    return logs, handles


def convert_logs_to_flat_logs(
    logs: Dict[str, Dict[str, Dict[str, Union[torch.Tensor, float]]]],
) -> Dict[str, Union[torch.Tensor, float]]:
    flat_logs = {}
    for module_name, components in logs.items():
        for component_name, stats in components.items():
            for metric_name, metric_value in stats.items():
                flat_logs[f"{module_name}:{component_name}:{metric_name}"] = metric_value

    return flat_logs
