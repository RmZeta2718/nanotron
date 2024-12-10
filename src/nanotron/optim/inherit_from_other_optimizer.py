from functools import cache
from typing import Callable, Dict, Optional, Set, Union

import torch

from nanotron.optim.base import BaseOptimizer, Optimizer, custom_load_state_dict


class InheritFromOtherOptimizer(BaseOptimizer):
    def __init__(self, optimizer: Optimizer, id_to_name: Dict[int, str]):
        self.id_to_name = id_to_name

        # if self.optimizer is from torch we replace load_state_dict with the one from torch
        if isinstance(optimizer, torch.optim.Optimizer):
            # Replace the load_state_dict method with our custom implementation that enables CPU offload
            original_load_state_dict = optimizer.load_state_dict
            optimizer.load_state_dict = (
                lambda state_dict, map_location=None: custom_load_state_dict(
                    optimizer, state_dict, map_location=map_location
                )
                if map_location is not None
                else original_load_state_dict(state_dict)
            )

        self.optimizer: Optimizer = optimizer

    def __getstate__(self):
        return self.optimizer.__getstate__()

    def __setstate__(self, state):
        return self.optimizer.__setstate__(state)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.optimizer.__repr__()})"

    def zero_grad(self):
        return self.optimizer.zero_grad()

    @cache
    def state_dict_additional_keys(self) -> Set[str]:
        if isinstance(self.optimizer, BaseOptimizer):
            return self.optimizer.state_dict_additional_keys()
        else:
            return set()

    def state_dict(self) -> dict:
        return self.optimizer.state_dict()

    def load_state_dict(self, state_dict: dict, map_location: Optional[Union[str, torch.device]] = None) -> None:
        return self.optimizer.load_state_dict(state_dict, map_location=map_location)

    def step(self, closure: Optional[Callable[[], float]] = None) -> Optional[float]:
        # NOTE: error: RuntimeError: params, grads, exp_avgs, and exp_avg_sqs must have same dtype, device, and layout
        # NOTE: add assert device, dtype, layout are the same

        params = [p for group in self.optimizer.param_groups for p in group["params"]]
        [p.grad for p in params if p.grad is not None]
        [state["exp_avg"] for state in self.optimizer.state_dict()["state"].values()]
        [state["exp_avg_sq"] for state in self.optimizer.state_dict()["state"].values()]

        # Check if all required attributes have the same device, dtype, and layout
        # ref_device = params[0].device
        # ref_dtype = params[0].dtype
        # ref_layout = params[0].layout

        # for attr_list, name in zip(
        #     [params, grads, exp_avgs, exp_avg_sqs],
        #     ["params", "grads", "exp_avgs", "exp_avg_sqs"]
        # ):
        #     for idx, attr in enumerate(attr_list):
        #         assert attr.device == ref_device, f"{name}[{idx}] has device {attr.device}, expected {ref_device}"
        #         assert attr.dtype == ref_dtype, f"{name}[{idx}] has dtype {attr.dtype}, expected {ref_dtype}"
        #         assert attr.layout == ref_layout, f"{name}[{idx}] has layout {attr.layout}, expected {ref_layout}"

        return self.optimizer.step(closure=closure)

    def get_base_optimizer(self):
        if isinstance(self.optimizer, torch.optim.Optimizer):
            return self.optimizer
        else:
            return self.optimizer.get_base_optimizer()

    @property
    def param_groups(self):
        return self.optimizer.param_groups

    def inherit_from(self, cls):
        if isinstance(self, cls):
            return True
        if isinstance(self.optimizer, InheritFromOtherOptimizer):
            return self.optimizer.inherit_from(cls)
        return False
