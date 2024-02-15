from dataclasses import dataclass

from nanotron.fp8.dtypes import DTypes


@dataclass
class FP8TensorRecipe:
    dtype: DTypes
    margin: int
    interval: int


@dataclass
class FP8LinearRecipe:
    input_grad: FP8TensorRecipe
    weight_grad: FP8TensorRecipe
    output_grad: FP8TensorRecipe


@dataclass
class FP8OptimRecipe:
    master_weight_dtype: DTypes
    exp_avg_dtype: DTypes
    exp_avg_sq_dtype: DTypes


@dataclass
class FP8TrainingRecipe:
    linear: FP8LinearRecipe
    optim: FP8OptimRecipe
