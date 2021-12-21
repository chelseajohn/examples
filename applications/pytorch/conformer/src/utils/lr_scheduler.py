# Copyright (c) 2021 Graphcore Ltd. All rights reserved.
# Copyright 2020 Johns Hopkins University (Shinji Watanabe)
#                Northwestern Polytechnical University (Pengcheng Guo)
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
# This file has been modified by Graphcore Ltd.
# Main change is modifying the part of parameters of WarmupLR class.

import torch
from torch.optim.lr_scheduler import _LRScheduler


class WarmupLR(_LRScheduler):
    """The WarmupLR scheduler

    This scheduler is almost same as NoamLR Scheduler except for following difference:

    NoamLR:
        lr = optimizer.lr * model_size ** -0.5
             * min(step ** -0.5, step * warmup_step ** -1.5)
    WarmupLR:
        lr = optimizer.lr * warmup_step ** 0.5
             * min(step ** -0.5, step * warmup_step ** -1.5)

    Note that the maximum lr equals to optimizer.lr in this scheduler.

    """

    def __init__(
        self,
        optimizer: torch.optim.Optimizer,
        adim,
        warmup_steps: int = 25000,
        last_epoch: int = -1,
    ):
        self.warmup_steps = warmup_steps
        self.adim = adim
        self.resume = False
        self.current_epoch = 0
        self.steps_per_epoch = 0
        # __init__() must be invoked before setting field
        # because step() is also invoked in __init__()
        super().__init__(optimizer, last_epoch)

    def __repr__(self):
        return f"{self.__class__.__name__}(warmup_steps={self.warmup_steps})"

    def get_lr(self):
        if not self.resume:
            step_num = self._step_count + 1
        else:
            step_num = self.steps_per_epoch * self.current_epoch + 1
            self._step_count = step_num
        return [lr * self.adim ** (-0.5) * min(step_num ** -0.5, step_num * self.warmup_steps ** -1.5) for lr in self.base_lrs]

    @property
    def global_step(self):
        return self._step_count