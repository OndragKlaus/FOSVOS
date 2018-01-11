from copy import deepcopy

import torch
import torch.nn as nn
import torch.nn.modules as modules
from torch.utils import model_zoo
from torchvision.models import resnet18, ResNet
from torchvision.models.resnet import BasicBlock, model_urls, Bottleneck

from layers.osvos_layers import interp_surgery, center_crop
from util.logger import get_logger

log = get_logger(__file__)


class OSVOS_RESNET(nn.Module):
    def __init__(self, pretrained: bool, version=18):
        self.inplanes = 64
        super(OSVOS_RESNET, self).__init__()
        log.info("Constructing OSVOS resnet architecture...")

        block, layers = self._match_version(version)

        layer0 = self._make_layer_base()
        layer1 = self._make_layer(block, 64, layers[0])
        layer2 = self._make_layer(block, 128, layers[1], stride=2)
        layer3 = self._make_layer(block, 256, layers[2], stride=2)
        layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.stages = modules.ModuleList([layer0, layer1, layer2, layer3, layer4])

        side_input_channels = [64, 128, 256, 512]
        side_prep = modules.ModuleList()
        score_dsn = modules.ModuleList()
        upscale = modules.ModuleList()
        upscale_ = modules.ModuleList()

        # Construct the network
        for index, channels in enumerate(side_input_channels):
            # Make the layers of the preparation step
            side_prep.append(nn.Conv2d(channels, 16, kernel_size=3, padding=1))

            # Make the layers of the score_dsn step
            upscale.append(nn.ConvTranspose2d(16, 16, kernel_size=2 ** (3 + index), stride=2 ** (2 + index),
                                              bias=False))
            score_dsn.append(nn.Conv2d(16, 1, kernel_size=1, padding=0))
            upscale_.append(nn.ConvTranspose2d(1, 1, kernel_size=2 ** (3 + index), stride=2 ** (2 + index),
                                               bias=False))

        self.side_prep = side_prep
        self.upscale = upscale
        self.score_dsn = score_dsn
        self.upscale_ = upscale_

        self.fuse = nn.Conv2d(64, 1, kernel_size=1, padding=0)

        # self._initialize_weights()
        # if pretrained:
        #     self._load_from_pytorch()

    def _match_version(self, version):
        if version == 18:
            block, layers = BasicBlock, [2, 2, 2, 2]
        elif version == 34:
            block, layers = BasicBlock, [3, 4, 6, 3]
        elif version == 50:
            block, layers = Bottleneck, [3, 4, 6, 3]
        elif version == 101:
            block, layers = Bottleneck, [3, 4, 23, 3]
        elif version == 152:
            block, layers = Bottleneck, [3, 8, 36, 3]
        else:
            raise Exception('Invalid version for resnet. Must be one of [18, 34, 50, 101, 152].')
        return block, layers

    def _make_layer_base(self) -> modules.Sequential:
        conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3, bias=False)
        bn1 = nn.BatchNorm2d(64)
        relu = nn.ReLU(inplace=True)
        maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        return modules.Sequential(conv1, bn1, relu, maxpool)

    def forward(self, x):
        # x = self.conv1(x)
        # x = self.bn1(x)
        # x = self.relu(x)
        # x = self.maxpool(x)

        # x = self.layer1(x)
        # x = self.layer2(x)
        # x = self.layer3(x)
        # x = self.layer4(x)

        # x = self.avgpool(x)
        # x = x.view(x.size(0), -1)
        # x = self.fc(x)

        # return x

        crop_h, crop_w = int(x.size()[-2]), int(x.size()[-1])
        x = self.stages[0](x)

        side = []
        side_out = []
        for (layer_stage, layer_side_prep, layer_score_dsn,
             layer_upscale, layer_upscale_) in zip(self.stages[1:], self.side_prep, self.score_dsn,
                                                   self.upscale, self.upscale_):
            x = layer_stage(x)
            temp_side_prep = layer_side_prep(x)

            temp_upscale = layer_upscale(temp_side_prep)
            temp_cropped = center_crop(temp_upscale, crop_h, crop_w)
            side.append(temp_cropped)

            temp_score_dsn = layer_score_dsn(temp_side_prep)
            temp_upscale_ = layer_upscale_(temp_score_dsn)
            temp_cropped_ = center_crop(temp_upscale_, crop_h, crop_w)
            side_out.append(temp_cropped_)

        out = torch.cat(side[:], dim=1)
        out = self.fuse(out)
        side_out.append(out)
        return side_out

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    @staticmethod
    def _make_layers_osvos(cfg, in_channels):
        layers = []
        for v in cfg:
            if v == 'M':
                layers.append(nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True))
            else:
                conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
                layers.extend([conv2d, nn.ReLU(inplace=True)])
                in_channels = v
        return nn.Sequential(*layers)

    def _initialize_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                m.weight.data.normal_(0, 0.001)
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()
            elif isinstance(m, nn.ConvTranspose2d):
                m.weight.data.zero_()
                m.weight.data = interp_surgery(m)

    def _load_from_pytorch(self) -> None:
        log.info('Loading weights from PyTorch Resnet')
        _resnet = resnet18(pretrained=True)

        inds = self._find_conv_layers(_resnet)
        k = 0
        for i in range(len(self.stages)):
            for j in range(len(self.stages[i])):
                if isinstance(self.stages[i][j], nn.Conv2d):
                    self.stages[i][j].weight = deepcopy(_resnet.features[inds[k]].weight)
                    self.stages[i][j].bias = deepcopy(_resnet.features[inds[k]].bias)
                    k += 1
                elif isinstance(self.stages[i][j], nn.BatchNorm2d):
                    self.stages[i][j].weight = deepcopy(_resnet.features[inds[k]].weight)
                    self.stages[i][j].bias = deepcopy(_resnet.features[inds[k]].bias)
                    k += 1

    @staticmethod
    def _find_conv_layers(_resnet):
        inds = []
        for i in range(len(_resnet.features)):
            if isinstance(_resnet.features[i], nn.Conv2d):
                inds.append(i)
            elif isinstance(_resnet.features[i], nn.BatchNorm2d):
                inds.append(i)
        return inds
