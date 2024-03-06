from enum import Enum, auto


# TODO(xrsrke): don't use plural
# TODO(xrsrke): change to QDType, so we don't mistaken it with the torch dtype
# QDType = Quantization DType
class DTypes(Enum):
    FP8E4M3 = auto()
    FP8E5M2 = auto()
    
    # TODO(xrsrke): move KFLOAT16 out of DTypes
    KFLOAT16 = auto()
    KFLOAT32 = auto()
