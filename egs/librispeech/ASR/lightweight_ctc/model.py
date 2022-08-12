import torch
import torch.nn as nn
from torch import Tensor


class MobileNetS4(nn.Module):
    """
    MobileNet V2-like network with subsampling rate = 4. The conv_subsampling layer is similar to Conv2dSubsampling, followed by configurable number of convolutional blocks, where each block consists of 3 bottleneck layers. 
    """

    def __init__(
        self,
        num_features: int,
        num_classes: int,
        subsampling_factor: int = 4,
        first_out_channels: int = 8,
        blocks: int = 3,
        expansion_rate: float = 4.0,
        skip_add: bool = True,
        rnn_dim: int = 0,
    ) -> None:
        """
        Args:
          num_features:
            The input dimension of the model.
          num_classes:
            The output dimension of the model.
          subsampling_factor:
            It reduces the number of output frames by this factor.
          first_out_channel:
            Out channels of the first layer. Scales network width.
          blocks:
            Each block halves width and doubles channels. Scales network depth.
          expansion_rate:
            Expansion rate of the bottleneck layers. Can be smaller or greater than 1.
          skip_add:
            Use skip connect in the bottleneck layers.
          rnn_dim:
            If positive, add a GRU layer after convolution with rnn_dim as the output dimension.
        """
        super().__init__()

        assert subsampling_factor == 4, 'Only subsampling = 4 supported.'
        self.num_features = num_features
        self.skip_add = skip_add
        self.rnn_dim = rnn_dim
        c = first_out_channels
        self.conv_subsample = nn.Sequential(
            nn.Conv2d(
                in_channels=1,
                out_channels=c,
                kernel_size=3,
                padding=1,
            ),
            nn.BatchNorm2d(c),
            nn.ReLU6(),
            nn.Conv2d(
                in_channels=c,
                out_channels=c*2,
                kernel_size=3,
                stride=2,
                padding=1,
            ),
            nn.BatchNorm2d(c*2),
            nn.ReLU6(),
            nn.Conv2d(
                in_channels=c*2,
                out_channels=c*4,
                kernel_size=3,
                stride=2,
                padding=1,
            ),
            nn.BatchNorm2d(c*4),
            nn.ReLU6(),
        )

        w = ((num_features+1)//2 + 1) // 2
        c *= 4
        self.bottleneck_layers = nn.Sequential()
        for _ in range(blocks):
            self.bottleneck_layers.append(
                Bottleneck(
                    in_channels=c,
                    out_channels=c*2,
                    expansion_rate=expansion_rate,
                    w_stride=2,
                    skip_add=False,
                )),
            self.bottleneck_layers.append(
                Bottleneck(
                    in_channels=c*2,
                    out_channels=c*2,
                    expansion_rate=expansion_rate,
                    skip_add=skip_add,
                )),
            self.bottleneck_layers.append(
                Bottleneck(
                    in_channels=c*2,
                    out_channels=c*2,
                    expansion_rate=expansion_rate,
                    skip_add=skip_add,
                )),
            c *= 2
            w = (w+1) // 2
        if rnn_dim > 0:
            self.rnn = nn.GRU(c*w, rnn_dim, batch_first=True)
            self.linear = nn.Linear(rnn_dim, num_classes)
        else:
            self.rnn = nn.Identity()
            self.linear = nn.Linear(c*w, num_classes)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
          x: input feature, with shape (N, 1, T, F), where F is the feature dimension.

        Returns:
          A tensor with shape (N, round(T / subsampling_factor), num_classes)
        """
        assert x.shape[-1] == self.num_features, f"Number of features should be {self.num_features} instead of {x.shape[3]}"
        x = self.conv_subsample(x)                      # N, 32, T//4, F//4
        x = self.bottleneck_layers(x)                   # N, Tout, Fout * Cout
        x = x.permute(0, 2, 3, 1).flatten(2)            # N, Tout, Fout * Cout
        if self.rnn_dim > 0:
            x, _ = self.rnn(x)
        x = self.linear(x)                              # N, Tout, num_classes
        x = nn.functional.log_softmax(x, dim=-1)
        return x


class Bottleneck(nn.Module):
    """
    Bottleneck layer adapted from MobileNet V2.
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        expansion_rate: float = 4.0,
        h_stride: int = 1,
        w_stride: int = 1,
        skip_add: bool = False,
    ) -> None:
        """
        Args:
          in_channels, out_channels: Number of input/output channels
          expansion_rate: Expansion rate of the bottleneck, can be greater or smaller than 1
          h_stride, w_stride: stride in height (time) and width dimension.
          skip_add: use skip connect like in MobileNet V2. Requires stride = 1 and in_channels = out_channels
        """

        super().__init__()
        if skip_add:
            assert h_stride == w_stride == 1 and in_channels == out_channels, "Using skip_add requires identical input/output shape."
        hidden_dim = round(in_channels * expansion_rate)
        self.skip_add = skip_add
        self.layer = nn.Sequential(
            # pointwise conv
            nn.Conv2d(in_channels, hidden_dim, 1),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU6(),
            # depthwise conv
            nn.Conv2d(hidden_dim, hidden_dim, 3,
                      (h_stride, w_stride), 1, groups=hidden_dim),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU6(),
            # pointwise conv
            nn.Conv2d(hidden_dim, out_channels, 1),
            nn.BatchNorm2d(out_channels),
        )

    def forward(self, x: Tensor) -> Tensor:
        if self.skip_add:
            return self.layer(x) + x
        else:
            return self.layer(x)