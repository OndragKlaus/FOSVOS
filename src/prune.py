# code for paper "Pruning Convolutional Neural Networks for Resource Efficient Inference"
# code adopted from https://github.com/eeric/channel_prune
# which itself is adopted from https://github.com/jacobgil/pytorch-pruning

from pathlib import Path
from typing import Optional, List, Tuple

import operator
import heapq
import argparse

import numpy as np
from tensorboardX import SummaryWriter
from torchvision.models.resnet import BasicBlock
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.utils import data
from torch import optim
from torch.autograd import Variable

from networks.osvos_resnet import OSVOS_RESNET, BasicBlockDummy
from util import io_helper, experiment_helper, gpu_handler
from layers.osvos_layers import class_balanced_cross_entropy_loss, center_crop
from util.logger import get_logger

log = get_logger(__file__)

N_MIN_CHANNELS = 4


def get_net(seq_name, train_offline: bool) -> nn.Module:
    net = OSVOS_RESNET(pretrained=True)
    # if train_offline:
    #     path_model = './models/resnet18_11_epoch-239.pth'
    # else:
    #     path_model = './models/resnet18_11_11_' + seq_name + '_epoch-9999.pth'
    # path_model = Path(path_model)
    # parameters = torch.load(str(path_model), map_location=lambda storage, loc: storage)
    # net.load_state_dict(parameters)
    net = gpu_handler.cast_cuda_if_possible(net)
    return net


def total_num_filters(net: OSVOS_RESNET) -> int:
    n_filters = 0
    for m in net.layer_base.modules():
        if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
            n_filters += m.out_channels
    for l in net.layer_stages:
        for b in l:
            if isinstance(b, BasicBlock) or isinstance(b, BasicBlockDummy):
                n_filters += b.conv1.out_channels
                n_filters += b.conv2.out_channels
    return n_filters


def total_num_filters_old(net: nn.Module) -> int:
    n_filters = 0
    for m in net.modules():
        if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
            n_filters += m.out_channels
        if n_filters > 0:
            return n_filters
    return n_filters


class FilterPruner:
    def __init__(self, net: nn.Module):
        self.net = net
        self.filter_ranks = {}
        self.activations = []
        self.gradients = []
        self.grad_index = 0
        self.activation_to_layer = {}
        self.skip_layer = []
        self.reset()

    def reset(self) -> None:
        self.filter_ranks = {}

    def forward(self, x):
        self.activations = []
        self.gradients = []
        self.grad_index = 0
        self.activation_to_layer = {}
        activation_index = 0
        kk = 0
        self.skip_layer = []

        crop_h, crop_w = int(x.size()[-2]), int(x.size()[-1])

        for l in self.net.layer_base:
            x = l(x)
            if isinstance(l, nn.modules.conv.Conv2d):
                x.register_hook(self.compute_rank)
                self.activations.append(x)
                self.activation_to_layer[activation_index] = kk
                if l.out_channels <= N_MIN_CHANNELS:
                    self.skip_layer.append(kk)
                activation_index += 1
                kk += 1
        # x = self.net.layer_base(x)

        side = []
        side_out = []
        for (layer_stage, layer_side_prep, layer_upscale_side_prep,
             layer_score_dsn, layer_upscale_score_dsn) in zip(self.net.layer_stages, self.net.side_prep,
                                                              self.net.upscale_side_prep,
                                                              self.net.score_dsn, self.net.upscale_score_dsn):
            # x = layer_stage(x)
            for kt in range(2):
                residual = x
                x = layer_stage[kt].conv1(x)
                x.register_hook(self.compute_rank)
                self.activations.append(x)
                self.activation_to_layer[activation_index] = kk
                if layer_stage[kt].conv1.out_channels <= N_MIN_CHANNELS:
                    self.skip_layer.append(kk)
                activation_index += 1
                kk += 1
                x = layer_stage[kt].bn1(x)
                x = layer_stage[kt].relu(x)
                x = layer_stage[kt].conv2(x)
                x.register_hook(self.compute_rank)
                self.activations.append(x)
                self.activation_to_layer[activation_index] = kk
                if layer_stage[kt].conv2.out_channels <= N_MIN_CHANNELS:
                    self.skip_layer.append(kk)
                activation_index += 1
                kk += 1
                x = layer_stage[kt].bn2(x)

                if layer_stage[kt].downsample is not None:
                    residual = layer_stage[kt].downsample[0](residual)
                    # TODO: figure out activation to layer
                    # x.register_hook(self.compute_rank)
                    # self.activations.append(x)
                    # self.activation_to_layer[activation_index] = kk
                    # activation_index += 1
                    # kk += 1
                    residual = layer_stage[kt].downsample[1](residual)
                x += residual
                x = layer_stage[kt].relu(x)

            temp_side_prep = layer_side_prep(x)

            temp_upscale = layer_upscale_side_prep(temp_side_prep)
            temp_cropped = center_crop(temp_upscale, crop_h, crop_w)
            side.append(temp_cropped)

            temp_score_dsn = layer_score_dsn(temp_side_prep)
            temp_upscale_ = layer_upscale_score_dsn(temp_score_dsn)
            temp_cropped_ = center_crop(temp_upscale_, crop_h, crop_w)
            side_out.append(temp_cropped_)

        out = torch.cat(side[:], dim=1)
        out = self.net.layer_fuse(out)
        side_out.append(out)
        return side_out

    def compute_rank(self, grad):
        activation_index = len(self.activations) - self.grad_index - 1
        activation = self.activations[activation_index]
        values = activation * grad
        values = values.sum(dim=3).sum(dim=2).sum(dim=0)
        values = values.data

        # Normalize the rank by the filter dimensions
        values = values / (activation.shape[0] * activation.shape[2] * activation.shape[3])

        if activation_index not in self.filter_ranks:
            default_value = torch.FloatTensor(values.shape[0]).zero_()
            self.filter_ranks[activation_index] = gpu_handler.cast_cuda_if_possible(default_value)

        self.filter_ranks[activation_index] += values
        self.grad_index += 1

    def normalize_ranks_per_layer(self):
        for i in self.filter_ranks:
            v = torch.abs(self.filter_ranks[i])
            divisor = np.sqrt(torch.sum(v * v))
            if divisor < 1e-5:
                log.info('filter norm is zero: {0}'.format(str(i)))
            else:
                v = v / divisor
            self.filter_ranks[i] = v.cpu()

    def lowest_ranking_filters(self, n_filters_to_prune_per_iter):
        data = []
        for i in sorted(self.filter_ranks.keys()):
            for j in range(self.filter_ranks[i].size(0)):
                index_layer = self.activation_to_layer[i]
                if index_layer in self.skip_layer:
                    log.info('Skipping layer {0}'.format(str(index_layer)))
                else:
                    data.append((index_layer, j, self.filter_ranks[i][j]))

        return heapq.nsmallest(n_filters_to_prune_per_iter, data, operator.itemgetter(2))

    def get_prunning_plan(self, n_filters_to_prune_per_iter):
        filters_to_prune = self.lowest_ranking_filters(n_filters_to_prune_per_iter)

        # After each of the k filters are prunned,
        # the filter index of the next filters change since the model is smaller.
        filters_to_prune_per_layer = {}
        for (l, f, _) in filters_to_prune:
            if l not in filters_to_prune_per_layer:
                filters_to_prune_per_layer[l] = []
            filters_to_prune_per_layer[l].append(f)

        for l in filters_to_prune_per_layer:
            filters_to_prune_per_layer[l] = sorted(filters_to_prune_per_layer[l])
            for i in range(len(filters_to_prune_per_layer[l])):
                filters_to_prune_per_layer[l][i] = filters_to_prune_per_layer[l][i] - i

        filters_to_prune = []
        for l in filters_to_prune_per_layer:
            for i in filters_to_prune_per_layer[l]:
                filters_to_prune.append((l, i))

        return filters_to_prune


def train_for_pruning(pruner: FilterPruner, dataloader: data.DataLoader, n_epochs: int, summary_writer: SummaryWriter,
                      iteration: int, is_offline: bool) -> None:
    epoch_start = iteration * n_epochs + 1
    epoch_end = epoch_start + n_epochs + 1
    for epoch in range(epoch_start, epoch_end):
        loss_epoch = 0.0
        for minibatch in dataloader:
            pruner.net.zero_grad()
            inputs, gts = minibatch['image'], minibatch['gt']
            inputs, gts = Variable(inputs, requires_grad=True), Variable(gts, requires_grad=False)
            inputs, gts = gpu_handler.cast_cuda_if_possible([inputs, gts])

            outputs = pruner.forward(inputs)
            if is_offline:
                losses = [0] * len(outputs)
                for i in range(0, len(outputs)):
                    losses[i] = class_balanced_cross_entropy_loss(outputs[i], gts, size_average=False)
                loss = sum(losses[:-1]) + losses[-1]  # type: Variable
            else:
                loss = class_balanced_cross_entropy_loss(outputs[-1], gts, size_average=False)

            loss_epoch += loss.data[0]
            loss.backward()

        loss_epoch /= len(dataloader.dataset)
        summary_writer.add_scalar('train_pruning/loss', loss_epoch, epoch)


def fine_tune(net: nn.Module, data_loader: data.DataLoader, n_epochs: int, summary_writer: SummaryWriter,
              iteration: int, is_offline: bool) -> None:
    optimizer = optim.Adam(net.parameters(), lr=1e-4, weight_decay=0.0002)

    epoch_start = iteration * n_epochs + 1
    epoch_end = epoch_start + n_epochs + 1
    for epoch in range(epoch_start, epoch_end):
        calculate_loss(epoch, net, data_loader, optimizer, summary_writer, is_offline)


def calculate_loss(epoch, net, dataloader, optimizer, summary_writer, is_offline: bool) -> None:
    net.train()

    loss_epoch = 0.0
    for minibatch in dataloader:
        optimizer.zero_grad()

        loss = _get_loss_minibatch(minibatch, net, is_offline)
        loss_epoch += loss.data[0]

        loss.backward()
        optimizer.step()

    loss_epoch /= len(dataloader.dataset)
    summary_writer.add_scalar('finetune/loss', loss_epoch, epoch)


def _get_loss_minibatch(minibatch, net: nn.Module, is_offline: bool) -> torch.FloatTensor:
    inputs, gts = minibatch['image'], minibatch['gt']
    inputs, gts = Variable(inputs, requires_grad=True), Variable(gts, requires_grad=False)
    inputs, gts = gpu_handler.cast_cuda_if_possible([inputs, gts])

    outputs = net.forward(inputs)
    if is_offline:
        losses = [0] * len(outputs)
        for i in range(0, len(outputs)):
            losses[i] = class_balanced_cross_entropy_loss(outputs[i], gts, size_average=False)
        loss = sum(losses[:-1]) + losses[-1]  # type: torch.FloatTensor
    else:
        loss = class_balanced_cross_entropy_loss(outputs[-1], gts, size_average=False)
    return loss


def prune_resnet18_conv_layer(net: OSVOS_RESNET, layer_index: int, filter_index: int) -> OSVOS_RESNET:
    # fix missing bias

    if layer_index == 0:
        conv_old = net.layer_base[0]
        batchnorm_old = net.layer_base[1]
        conv_next_old = net.layer_stages[0][0].conv1
        downsample_1_old = net.layer_stages[0][0].downsample

        if conv_old.out_channels == 1:
            return net

        conv_new = prune_convolution(conv_old, filter_index, is_reducing_channels_out=True, layer_index=layer_index,
                                     net=net)
        batchnorm_new = prune_batchnorm(batchnorm_old, filter_index, layer_index=layer_index, net=net)
        conv_next_new = prune_convolution(conv_next_old, filter_index, is_reducing_channels_out=False,
                                          layer_index=layer_index, net=net)
        if downsample_1_old is None:
            n_channels_out = net.layer_stages[0][0].bn2.num_features
            downsample_1_new = nn.Sequential(nn.Conv2d(conv_next_new.in_channels, n_channels_out,
                                                       kernel_size=1, stride=1, bias=False),
                                             nn.BatchNorm2d(n_channels_out))
            init_downsample(downsample_1_new)
        else:
            conv_downsample_1_new = prune_convolution(downsample_1_old[0], filter_index, is_reducing_channels_out=False,
                                                      layer_index=layer_index, net=net)
            downsample_1_new = nn.Sequential(conv_downsample_1_new, downsample_1_old[1])

        net.layer_base = nn.Sequential(conv_new, batchnorm_new, *list(net.layer_base.children())[2:])

        bb_old = net.layer_stages[0][0]
        bb_new = BasicBlockDummy(conv_next_new, bb_old.bn1, bb_old.relu, bb_old.conv2, bb_old.bn2, downsample_1_new,
                                 bb_old.stride)
        net.layer_stages[0] = nn.Sequential(bb_new, net.layer_stages[0][1])

    else:
        index_stage = (layer_index - 1) // 4
        index_block = (layer_index - 1) % 4
        if index_block == 0:
            # don't need to change downsample because the [0]conv1.in_channels and [1].conv2.out_channels stay the same
            conv_old = net.layer_stages[index_stage][0].conv1
            batchnorm_old = net.layer_stages[index_stage][0].bn1
            conv_next_old = net.layer_stages[index_stage][0].conv2

            if conv_old.out_channels == 1:
                return net

            conv_new = prune_convolution(conv_old, filter_index, is_reducing_channels_out=True, layer_index=layer_index,
                                         net=net)
            batchnorm_new = prune_batchnorm(batchnorm_old, filter_index, layer_index=layer_index, net=net)
            conv_next_new = prune_convolution(conv_next_old, filter_index, is_reducing_channels_out=False,
                                              layer_index=layer_index, net=net)

            bb_old = net.layer_stages[index_stage][0]
            bb_new = BasicBlockDummy(conv_new, batchnorm_new, bb_old.relu, conv_next_new, bb_old.bn2, bb_old.downsample,
                                     bb_old.stride)
            net.layer_stages[index_stage] = nn.Sequential(bb_new, net.layer_stages[index_stage][1])
        elif index_block == 1:
            conv_old = net.layer_stages[index_stage][0].conv2
            batchnorm_old = net.layer_stages[index_stage][0].bn2
            conv_next_old = net.layer_stages[index_stage][1].conv1
            downsample_1_old = net.layer_stages[index_stage][0].downsample

            if conv_old.out_channels == 1:
                return net

            conv_new = prune_convolution(conv_old, filter_index, is_reducing_channels_out=True, layer_index=layer_index,
                                         net=net)
            batchnorm_new = prune_batchnorm(batchnorm_old, filter_index, layer_index=layer_index, net=net)
            conv_next_new = prune_convolution(conv_next_old, filter_index, is_reducing_channels_out=False,
                                              layer_index=layer_index, net=net)

            if downsample_1_old is None:
                n_channels_out = batchnorm_new.num_features
                downsample_1_new = nn.Sequential(nn.Conv2d(net.layer_stages[index_stage][0].conv1.in_channels,
                                                           n_channels_out, kernel_size=1, stride=1, bias=False),
                                                 nn.BatchNorm2d(n_channels_out))
                init_downsample(downsample_1_new)
            else:
                conv_downsample_1_new = prune_convolution(downsample_1_old[0], filter_index,
                                                          is_reducing_channels_out=True, layer_index=layer_index,
                                                          net=net)
                batchnorm_downsample_1_new = prune_batchnorm(downsample_1_old[1], filter_index, layer_index=layer_index,
                                                             net=net)
                downsample_1_new = nn.Sequential(conv_downsample_1_new, batchnorm_downsample_1_new)

            bb_1_old = net.layer_stages[index_stage][0]
            bb_1_new = BasicBlockDummy(bb_1_old.conv1, bb_1_old.bn1, bb_1_old.relu, conv_new, batchnorm_new,
                                       downsample_1_new, bb_1_old.stride)

            downsample_2_old = net.layer_stages[index_stage][1].downsample
            if downsample_2_old is None:
                n_channels_out = net.layer_stages[index_stage][1].bn2.num_features
                downsample_2_new = nn.Sequential(nn.Conv2d(conv_next_new.in_channels, n_channels_out,
                                                           kernel_size=1, stride=1, bias=False),
                                                 nn.BatchNorm2d(n_channels_out))
                init_downsample(downsample_2_new)
            else:
                conv_downsample_2_new = prune_convolution(downsample_2_old[0], filter_index,
                                                          is_reducing_channels_out=False, layer_index=layer_index,
                                                          net=net)
                downsample_2_new = nn.Sequential(conv_downsample_2_new, downsample_2_old[1])

            bb_2_old = net.layer_stages[index_stage][1]
            bb_2_new = BasicBlockDummy(conv_next_new, bb_2_old.bn1, bb_2_old.relu, bb_2_old.conv2, bb_2_old.bn2,
                                       downsample_2_new, bb_2_old.stride)
            net.layer_stages[index_stage] = nn.Sequential(bb_1_new, bb_2_new)

        elif index_block == 2:
            # don't need to change downsample because the [0]conv1.in_channels and [1].conv2.out_channels stay the same
            conv_old = net.layer_stages[index_stage][1].conv1
            batchnorm_old = net.layer_stages[index_stage][1].bn1
            conv_next_old = net.layer_stages[index_stage][1].conv2

            if conv_old.out_channels == 1:
                return net

            conv_new = prune_convolution(conv_old, filter_index, is_reducing_channels_out=True, layer_index=layer_index,
                                         net=net)
            batchnorm_new = prune_batchnorm(batchnorm_old, filter_index, layer_index=layer_index, net=net)
            conv_next_new = prune_convolution(conv_next_old, filter_index, is_reducing_channels_out=False,
                                              layer_index=layer_index, net=net)

            bb_old = net.layer_stages[index_stage][1]
            bb_new = BasicBlockDummy(conv_new, batchnorm_new, bb_old.relu, conv_next_new, bb_old.bn2, bb_old.downsample,
                                     bb_old.stride)
            net.layer_stages[index_stage] = nn.Sequential(net.layer_stages[index_stage][0], bb_new)
        else:
            conv_old = net.layer_stages[index_stage][1].conv2
            batchnorm_old = net.layer_stages[index_stage][1].bn2
            downsample_1_old = net.layer_stages[index_stage][1].downsample

            if conv_old.out_channels == 1:
                return net

            conv_new = prune_convolution(conv_old, filter_index, is_reducing_channels_out=True, layer_index=layer_index,
                                         net=net)
            batchnorm_new = prune_batchnorm(batchnorm_old, filter_index, layer_index=layer_index, net=net)

            if downsample_1_old is None:
                n_channels_out = conv_new.out_channels
                downsample_1_new = nn.Sequential(nn.Conv2d(net.layer_stages[index_stage][1].conv1.in_channels,
                                                           n_channels_out, kernel_size=1, stride=1, bias=False),
                                                 nn.BatchNorm2d(n_channels_out))
                init_downsample(downsample_1_new)
            else:
                conv_downsample_1_new = prune_convolution(downsample_1_old[0], filter_index,
                                                          is_reducing_channels_out=True, layer_index=layer_index,
                                                          net=net)
                batchnorm_downsample_1_new = prune_batchnorm(downsample_1_old[1], filter_index, layer_index=layer_index,
                                                             net=net)
                downsample_1_new = nn.Sequential(conv_downsample_1_new, batchnorm_downsample_1_new)

            bb_old = net.layer_stages[index_stage][1]
            bb_new = BasicBlockDummy(bb_old.conv1, bb_old.bn1, bb_old.relu, conv_new, batchnorm_new, downsample_1_new,
                                     bb_old.stride)
            net.layer_stages[index_stage] = nn.Sequential(net.layer_stages[index_stage][0], bb_new)

            net.side_prep[index_stage] = prune_convolution(net.side_prep[index_stage], filter_index,
                                                           is_reducing_channels_out=False, layer_index=layer_index,
                                                           net=net)

            if index_stage <= 2:
                conv_next_old = net.layer_stages[index_stage + 1][0].conv1
                conv_next_new = prune_convolution(conv_next_old, filter_index, is_reducing_channels_out=False,
                                                  layer_index=layer_index, net=net)
                downsample_2_old = net.layer_stages[index_stage + 1][0].downsample

                if downsample_2_old is None:
                    n_channels_out = net.layer_stages[index_stage + 1][0].bn2.num_features
                    downsample_2_new = nn.Sequential(nn.Conv2d(conv_next_new.in_channels, n_channels_out,
                                                               kernel_size=1, stride=1, bias=False),
                                                     nn.BatchNorm2d(n_channels_out))
                    init_downsample(downsample_2_new)
                else:
                    conv_downsample_2_new = prune_convolution(downsample_2_old[0], filter_index,
                                                              is_reducing_channels_out=False, layer_index=layer_index,
                                                              net=net)
                    downsample_2_new = nn.Sequential(conv_downsample_2_new, downsample_2_old[1])
                bb_old = net.layer_stages[index_stage + 1][0]
                bb_new = BasicBlockDummy(conv_next_new, bb_old.bn1, bb_old.relu, bb_old.conv2, bb_old.bn2,
                                         downsample_2_new, bb_old.stride)
                net.layer_stages[index_stage + 1] = nn.Sequential(bb_new, net.layer_stages[index_stage + 1][1])

    return net


def init_downsample(downsample):
    downsample[0].weight.data.normal_(0, 0.001)
    downsample[1].weight.data.fill_(1)
    downsample[1].bias.data.zero_()


def prune_convolution(conv, filter_index, is_reducing_channels_out: bool, layer_index, net):
    reduction_channels_in = 0 if is_reducing_channels_out else 1
    reduction_channels_out = 1 if is_reducing_channels_out else 0
    new_conv = torch.nn.Conv2d(in_channels=conv.in_channels - reduction_channels_in,
                               out_channels=conv.out_channels - reduction_channels_out,
                               kernel_size=conv.kernel_size,
                               stride=conv.stride,
                               padding=conv.padding,
                               dilation=conv.dilation,
                               groups=conv.groups,
                               bias=False)
    old_weights = conv.weight.data.cpu().numpy()
    new_weights = new_conv.weight.data.cpu().numpy()

    log.debug('layer_index %d, filter_index %d', layer_index, filter_index)
    if is_reducing_channels_out:
        new_weights[:filter_index, :, :, :] = old_weights[:filter_index, :, :, :]
        new_weights[filter_index:, :, :, :] = old_weights[filter_index + 1:, :, :, :]
    else:
        new_weights[:, :filter_index, :, :] = old_weights[:, :filter_index, :, :]
        new_weights[:, filter_index:, :, :] = old_weights[:, filter_index + 1:, :, :]

    new_conv.weight.data = gpu_handler.cast_cuda_if_possible(torch.from_numpy(new_weights))
    # new_conv.weight.data = torch.from_numpy(new_weights)
    return new_conv


def prune_batchnorm(batchnorm_old, filter_index, layer_index, net):
    new_batchnorm = nn.BatchNorm2d(num_features=batchnorm_old.num_features - 1,
                                   eps=batchnorm_old.eps,
                                   momentum=batchnorm_old.momentum,
                                   affine=batchnorm_old.affine)
    # net.layer_base[1].track_running_stats no attribute...
    old_weights = batchnorm_old.weight.data.cpu().numpy()
    new_weights = new_batchnorm.weight.data.cpu().numpy()
    new_weights[:filter_index] = old_weights[:filter_index]
    new_weights[filter_index:] = old_weights[filter_index + 1:]
    new_batchnorm.weight.data = gpu_handler.cast_cuda_if_possible(torch.from_numpy(new_weights))
    return new_batchnorm


# net.layer_base[1]

def get_candidates_to_prune(net: nn.Module, n_filters_to_prune: int, dataloader: data.DataLoader,
                            n_epochs_select: int, summary_writer: SummaryWriter,
                            iterations: int, is_offline_mode: bool) -> List[Tuple[int, int]]:
    pruner = FilterPruner(net)
    train_for_pruning(pruner, dataloader, n_epochs_select, summary_writer, iterations, is_offline_mode)
    pruner.normalize_ranks_per_layer()
    return pruner.get_prunning_plan(n_filters_to_prune)


class DummyProvider:
    def __init__(self, net):
        self.network = net


def get_experiment_id(n_epochs_select: int, n_epochs_finetune: int, prune_per_iter: int) -> str:
    format_string = 'prune_per_iter={0},epochs_select={1},epochs_finetune={2}'
    return format_string.format(prune_per_iter, n_epochs_select, n_epochs_finetune)


def main(n_epochs_select: int, n_epochs_finetune: int, prune_per_iter: int, sequence_name: Optional[str] = None,
         is_offline_mode: bool = False) -> None:
    percentage_prune_max = 90
    percentage_prune_steps = 10

    experiment_id = get_experiment_id(n_epochs_select, n_epochs_finetune, prune_per_iter)
    log.info('Experiment ID: %s', experiment_id)
    path_stem = 'resnet18/11'
    path_stem += '/' + 'prune'
    path_stem += '/' + experiment_id
    path_stem += '/' + ('offline' if is_offline_mode else 'online')
    log.info('Path stem: %s', str(path_stem))

    path_output_model_base = Path('models') / path_stem
    path_output_model_base.mkdir(parents=True, exist_ok=True)

    path_tensorboard = Path('tensorboard') / path_stem
    summary_writer = io_helper.get_summary_writer(path_tensorboard)

    net = get_net(sequence_name, is_offline_mode)
    n_filters_start = total_num_filters(net)
    n_filters_to_prune_per_iter = prune_per_iter
    n_iterations = 1 + int(n_filters_start / n_filters_to_prune_per_iter * percentage_prune_steps / 100)

    log.info('Filters in model: %d', n_filters_start)
    log.info('Pruning maximal percentage: %d', percentage_prune_max)
    log.info('Output every percentage: %d', percentage_prune_steps)
    log.info('Number of iterations per percentage step: %d', n_iterations)
    log.info('Prune n filters per iteration: %d', n_filters_to_prune_per_iter)

    dataloader_train = io_helper.get_data_loader_train(Path('/usr/stud/ondrag/DAVIS'), batch_size=1,
                                                       seq_name=sequence_name)
    dataloader_test = io_helper.get_data_loader_test(Path('/usr/stud/ondrag/DAVIS'), batch_size=1,
                                                     seq_name=sequence_name)

    fine_tune_calls = 0
    for percentage in range(percentage_prune_steps, percentage_prune_max + 1, percentage_prune_steps):
        n_filters = total_num_filters(net)
        log.info('Remaining filters in model: %d', n_filters)
        log.info('Pruned percentage so far: %d', 100 * (1 - n_filters / n_filters_start))
        log.info('Pruning to percentage: %d', percentage)
        log.debug('Plan to prune %d...%s', 0, str(net))

        for index_iteration in tqdm(range(n_iterations)):
            prune_targets = get_candidates_to_prune(net, n_filters_to_prune_per_iter, dataloader_train,
                                                    n_epochs_select, summary_writer, fine_tune_calls, is_offline_mode)

            # net = net.cpu()
            layer_index_prev = -1
            for layer_index, filter_index in prune_targets:
                if layer_index != layer_index_prev:
                    log.debug(layer_index_prev, net)
                    layer_index_prev = layer_index
                net = prune_resnet18_conv_layer(net, layer_index, filter_index)

            net = gpu_handler.cast_cuda_if_possible(net)
            log.debug('Plan to prune %d...%s', index_iteration, str(net))

            fine_tune(net, dataloader_train, args.n_epochs_finetune, summary_writer, fine_tune_calls, is_offline_mode)
            fine_tune_calls += 1

        if is_offline_mode:
            path_output_model = path_output_model_base / str(percentage) / 'offline'
            path_output_model.mkdir(parents=True, exist_ok=True)
            path_output_model /= '240.pth'
        else:
            path_output_model = path_output_model_base / str(percentage) / sequence_name
            path_output_model.mkdir(parents=True, exist_ok=True)
            path_output_model /= '10000.pth'

        log.info('Saving model to %s', str(path_output_model))
        torch.save(net, str(path_output_model))

        net_provider = DummyProvider(net)

        if is_offline_mode:
            path_output_images = Path('results') / path_stem / str(percentage) / 'offline'
        else:
            path_output_images = path_output_model_base / str(percentage) / sequence_name

        log.info('Saving images to %s', str(path_output_images))

        # first time to measure the speed
        experiment_helper.test(net_provider, dataloader_test, path_output_images, is_visualizing_results=False,
                               eval_speeds=True, seq_name=sequence_name)

        # second time for image output
        experiment_helper.test(net_provider, dataloader_test, path_output_images, is_visualizing_results=False,
                               eval_speeds=False, seq_name=sequence_name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--gpu-id', default=None, type=int, help='The gpu id to use')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('-s', '--sequence-name', default=None, type=Optional[str])
    parser.add_argument('-sg', '--sequence-group', default=None, type=Optional[int])
    parser.add_argument('-sgs', '--sequence-group-size', default=None, type=Optional[int])

    parser.add_argument('--n-epochs-select', default=20, type=int, help='version to try')
    parser.add_argument('--n-epochs-finetune', default=20, type=int, help='version to try')
    parser.add_argument('--prune-per-iter', default=64, type=int, help='filters to prune per iteration')

    args = parser.parse_args()

    gpu_handler.select_gpu(args.gpu_id)

    # args.offline = True
    # args.n_epochs_select = 1
    # args.n_epochs_finetune = 1

    if args.offline:
        seq_name = None

    if not args.offline and args.sequence_name is None:
        sequences_val = ['blackswan', 'bmx-trees', 'breakdance', 'camel', 'car-roundabout', 'car-shadow', 'cows',
                         'dance-twirl', 'dog', 'drift-chicane', 'drift-straight', 'goat', 'horsejump-high', 'kite-surf',
                         'libby', 'motocross-jump', 'paragliding-launch', 'parkour', 'scooter-black', 'soapbox']

        sequences_train = ['bear', 'bmx-bumps', 'boat', 'breakdance-flare', 'bus', 'car-turn', 'dance-jump',
                           'dog-agility', 'drift-turn', 'elephant', 'flamingo', 'hike', 'hockey', 'horsejump-low',
                           'kite-walk', 'lucia', 'mallard-fly', 'mallard-water', 'motocross-bumps', 'motorbike',
                           'paragliding', 'rhino', 'rollerblade', 'scooter-gray', 'soccerball', 'stroller', 'surf',
                           'swing', 'tennis', 'train']

        sequences_all = list(set(sequences_train + sequences_val))

        if args.sequence_group is None:
            already_done = []
            sequences = [s
                         for s in sequences_val
                         if s not in already_done]
        else:
            sequences = [s
                         for i, s in enumerate(sequences_val)
                         if i % args.sequence_group_size == args.sequence_group]

        [main(args.n_epochs_select, args.n_epochs_finetune, args.prune_per_iter, s, args.offline)
         for s in sequences]

    else:
        main(args.n_epochs_select, args.n_epochs_finetune, args.prune_per_iter, args.sequence_name, args.offline)
