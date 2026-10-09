"""Inference-only ResNet50 U-Net matching the supplied TC4 checkpoint.

The encoder uses the original ceil-mode, zero-padding max pool. torchvision's
standard ResNet pool is different and must not silently replace this operation.
"""
import torch
from torch import nn
from torchvision.models.resnet import Bottleneck, ResNet


class Encoder(ResNet):
    def __init__(self):
        super().__init__(Bottleneck, [3, 4, 6, 3])
        self.maxpool = nn.MaxPool2d(3, 2, padding=0, ceil_mode=True)
        del self.avgpool
        del self.fc

    def forward(self, x):
        first = self.relu(self.bn1(self.conv1(x)))
        second = self.layer1(self.maxpool(first))
        third = self.layer2(second)
        fourth = self.layer3(third)
        return first, second, third, fourth, self.layer4(fourth)


class Up(nn.Module):
    def __init__(self, inputs, outputs):
        super().__init__()
        self.conv1 = nn.Conv2d(inputs, outputs, 3, padding=1)
        self.conv2 = nn.Conv2d(outputs, outputs, 3, padding=1)
        self.up = nn.UpsamplingBilinear2d(scale_factor=2)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, skip, x):
        x = torch.cat([skip, self.up(x)], 1)
        return self.relu(self.conv2(self.relu(self.conv1(x))))


class TC4UNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.resnet = Encoder()
        self.up_concat4 = Up(3072, 512)
        self.up_concat3 = Up(1024, 256)
        self.up_concat2 = Up(512, 128)
        self.up_concat1 = Up(192, 64)
        self.up_conv = nn.Sequential(nn.UpsamplingBilinear2d(scale_factor=2),
            nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(),
            nn.Conv2d(64, 64, 3, padding=1), nn.ReLU())
        self.final = nn.Conv2d(64, 2, 1)

    def forward(self, x):
        a, b, c, d, e = self.resnet(x)
        x = self.up_concat1(a, self.up_concat2(b,
            self.up_concat3(c, self.up_concat4(d, e))))
        return self.final(self.up_conv(x))
