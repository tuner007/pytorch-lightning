# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Decorator for LightningModule methods."""

from functools import wraps
from typing import Callable, Dict, Optional

from pytorch_lightning.overrides import LightningDistributedModule
from pytorch_lightning.utilities import rank_zero_warn


def parameter_validation(fn: Callable) -> Callable:
    """Validates that the module parameter lengths match after moving to the device. It is useful when tying
    weights on TPU's.

    Args:
        fn: ``model_to_device`` method

    Note:
        TPU's require weights to be tied/shared after moving the module to the device.
        Failure to do this results in the initialization of new weights which are not tied.
        To overcome this issue, weights should be tied using the ``on_post_move_to_device`` model hook
        which is called after the module has been moved to the device.

    See Also:
        - `XLA Documentation <https://github.com/pytorch/xla/blob/master/TROUBLESHOOTING.md#xla-tensor-quirks>`_
    """

    @wraps(fn)
    def inner_fn(self, *args, **kwargs):
        pre_layer_count = len(list(self.model.parameters()))
        module = fn(self, *args, **kwargs)
        self.model.on_post_move_to_device()
        post_layer_count = len(list(self.model.parameters()))

        if not pre_layer_count == post_layer_count:
            rank_zero_warn(
                "The model layers do not match after moving to the target device."
                " If your model employs weight sharing on TPU,"
                " please tie your weights using the `on_post_move_to_device` model hook.\n"
                f"Layer count: [Before: {pre_layer_count} After: {post_layer_count}]"
            )

        return module

    return inner_fn


def find_shared_parameters(module, tied_parameters: Optional[Dict] = None, prefix: str = ""):
    if tied_parameters is None:
        first_call = True
        tied_parameters = {}
    else:
        first_call = False
    for name, param in module._parameters.items():
        param_prefix = prefix + ("." if prefix else "") + name
        if param is None:
            continue
        if param not in tied_parameters:
            tied_parameters[param] = []
        tied_parameters[param].append(param_prefix)
    for name, m in module._modules.items():
        if m is None:
            continue
        submodule_prefix = prefix + ("." if prefix else "") + name
        find_shared_parameters(m, tied_parameters, submodule_prefix)
    if first_call:
        return [x for x in tied_parameters.values() if len(x) > 1]


def apply_weight_tying(module, shared_params):
    for shared_param in shared_params:
        ref = _get_module_by_path(module, shared_param[0])
        for path in shared_param[1:]:
            _set_module_by_path(module, path, ref)
    return module


def _get_module_by_path(module, path):
    path = path.split(".")
    for name in path:
        module = getattr(module, name)
    return module


def _set_module_by_path(module, path, value):
    path = path.split(".")
    for name in path[:-1]:
        module = getattr(module, name)
    setattr(module, path[-1], value)


def auto_weight_tying(model_to_device: Callable) -> Callable:
    @wraps(model_to_device)
    def inner_fn(self, *args, **kwargs):
        shared_params = find_shared_parameters(self.model)
        model_to_device(self, *args, **kwargs)
        module = self.model.module if isinstance(self.model, LightningDistributedModule) else self.model
        module = apply_weight_tying(module, shared_params)
        return module

    return inner_fn
