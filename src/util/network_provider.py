from pathlib import Path
from typing import Optional, Dict, Type, Tuple
from abc import ABC, abstractmethod

import torch
from torch import optim
from torch.optim import Optimizer

from networks.osvos_resnet import OSVOS_RESNET
from networks.osvos_vgg import OSVOS_VGG
from util import gpu_handler
from util.logger import get_logger
from .settings import Settings, OfflineSettings, OnlineSettings

log = get_logger(__file__)


class NetworkProvider(ABC):

    def __init__(self, name: str, save_dir: Tuple[Path, Path], network_type: type, settings: Settings,
                 variant_offline: Optional[int] = None, variant_online: Optional[int] = None) -> None:
        self.name = name
        self.save_dir = save_dir
        self.network_type = network_type
        self._settings = settings
        self.variant_offline = variant_offline
        self.variant_online = variant_online
        self.network = None

    def init_network(self, **kwargs) -> object:
        net = self.network_type(**kwargs)
        net = gpu_handler.cast_cuda_if_possible(net, verbose=True)
        self.network = net
        return net

    def _get_file_path(self, epoch: int, sequence: Optional[str] = None) -> Path:
        model_name = self.name
        if self.variant_offline is not None:
            model_name += '_' + str(self.variant_offline)
        if sequence is not None:
            if self.variant_online is not None:
                model_name += '_' + str(self.variant_online)
            model_name += '_' + sequence

        x = ''
        # x = '_offline_min_30_32_3_3'
        # x = '_offline_min_50_32_3_3'
        # x = '_offline_min_70_32_3_3'

        file_path = self.save_dir / '{0}_epoch-{1}{2}.pth'.format(model_name, str(epoch), x)
        return file_path

    def load_model(self, epoch: int, sequence: Optional[str] = None) -> None:
        model_path = str(self.save_dir[0])
        log.info("Loading weights from: {0}".format(model_path))
        # self.network = torch.load(str(file_path))
        self.network.load_state_dict(torch.load(model_path, map_location=lambda storage, loc: storage))
        self.network = gpu_handler.cast_cuda_if_possible(self.network, verbose=True)

    def save_model(self, epoch: int, sequence: Optional[str] = None) -> None:
        file_path = str(self._get_file_path(epoch, sequence))
        log.info("Saving weights to: {0}".format(file_path))
        torch.save(self.network, file_path)

    @abstractmethod
    def load_network_train(self) -> None:
        pass

    @abstractmethod
    def load_network_test(self, sequence: Optional[str] = None) -> None:
        pass

    @abstractmethod
    def get_optimizer(self) -> optim.SGD:
        pass


class VGGOfflineProvider(NetworkProvider):

    def __init__(self, name: str, save_dir: Path, settings: OfflineSettings, variant_offline: Optional[int] = None):
        super(VGGOfflineProvider, self).__init__(name=name, save_dir=save_dir, settings=settings,
                                                 network_type=OSVOS_VGG, variant_offline=variant_offline)

    def load_network_train(self) -> None:
        if self._settings.start_epoch == 0:
            if self._settings.is_loading_vgg_caffe:
                self.init_network(pretrained=2)
            else:
                self.init_network(pretrained=1)
        else:
            self.init_network(pretrained=0)
            self.load_model(self._settings.start_epoch)

    def load_network_test(self, sequence: Optional[str] = None) -> None:
        self.init_network(pretrained=0)
        self.load_model(self._settings.n_epochs, sequence=sequence)

    def get_optimizer(self, learning_rate: float = 1e-8, weight_decay: float = 0.0002,
                      momentum: float = 0.9) -> optim.SGD:
        net = self.network
        optimizer = optim.SGD([
            {'params': [pr[1] for pr in net.stages.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay,
             'initial_lr': learning_rate},
            {'params': [pr[1] for pr in net.stages.named_parameters() if 'bias' in pr[0]], 'lr': 2 * learning_rate,
             'initial_lr': 2 * learning_rate},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay,
             'initial_lr': learning_rate},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]], 'lr': 2 * learning_rate,
             'initial_lr': 2 * learning_rate},
            {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
             'lr': learning_rate / 10,
             'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
            {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate / 10,
             'initial_lr': 2 * learning_rate / 10},
            {'params': [pr[1] for pr in net.upscale.named_parameters() if 'weight' in pr[0]], 'lr': 0, 'initial_lr': 0},
            {'params': [pr[1] for pr in net.upscale_.named_parameters() if 'weight' in pr[0]], 'lr': 0,
             'initial_lr': 0},
            {'params': net.fuse.weight, 'lr': learning_rate / 100, 'initial_lr': learning_rate / 100,
             'weight_decay': weight_decay},
            {'params': net.fuse.bias, 'lr': 2 * learning_rate / 100, 'initial_lr': 2 * learning_rate / 100},
        ], lr=learning_rate, momentum=momentum)
        return optimizer


class VGGOnlineProvider(NetworkProvider):

    def __init__(self, name: str, save_dir: Path, settings: OnlineSettings, variant_offline: Optional[int] = None,
                 variant_online: Optional[int] = None):
        super(VGGOnlineProvider, self).__init__(name=name, save_dir=save_dir, settings=settings,
                                                network_type=OSVOS_VGG, variant_offline=variant_offline,
                                                variant_online=variant_online)

    def load_network_train(self) -> None:
        self.init_network(pretrained=0)
        self.load_model(self._settings.offline_epoch)

    def load_network_test(self, sequence: Optional[str] = None) -> None:
        self.init_network(pretrained=0)
        self.load_model(self._settings.n_epochs, sequence=sequence)

    def get_optimizer(self, learning_rate: float = 1e-8, weight_decay: float = 0.0002,
                      momentum: float = 0.9) -> optim.SGD:
        net = self.network
        optimizer = optim.SGD([
            {'params': [pr[1] for pr in net.stages.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay},
            {'params': [pr[1] for pr in net.stages.named_parameters() if 'bias' in pr[0]], 'lr': learning_rate * 2},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]], 'lr': learning_rate * 2},
            {'params': [pr[1] for pr in net.upscale.named_parameters() if 'weight' in pr[0]], 'lr': 0},
            {'params': [pr[1] for pr in net.upscale_.named_parameters() if 'weight' in pr[0]], 'lr': 0},
            {'params': net.fuse.weight, 'lr': learning_rate / 100, 'weight_decay': weight_decay},
            {'params': net.fuse.bias, 'lr': 2 * learning_rate / 100},
        ], lr=learning_rate, momentum=momentum)
        return optimizer


class ResNetOfflineProvider(NetworkProvider):

    def __init__(self, name: str, save_dir: Path, settings: OfflineSettings, variant_offline: Optional[int] = None,
                 version: int = 18):
        super(ResNetOfflineProvider, self).__init__(name=name, save_dir=save_dir, settings=settings,
                                                    network_type=OSVOS_RESNET, variant_offline=variant_offline)
        self.version = version

    def load_network_train(self) -> None:
        if self._settings.start_epoch == 0:
            self.init_network(pretrained=True, version=self.version)
        else:
            self.init_network(pretrained=False, version=self.version)
            self.load_model(self._settings.start_epoch)

    def load_network_test(self, sequence: Optional[str] = None) -> None:
        self.init_network(pretrained=False, version=self.version)
        self.load_model(self._settings.n_epochs, sequence=sequence)

    def get_optimizer(self, learning_rate: float = 1e-8, weight_decay: float = 0.0002,
                      momentum: float = 0.9) -> Optimizer:
        net = self.network
        default_var = optim.SGD([
            {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay, 'initial_lr': learning_rate},
            {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay, 'initial_lr': learning_rate},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
            {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
             'lr': learning_rate / 10, 'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
            {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate / 10, 'initial_lr': 2 * learning_rate / 10},
            {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
             'lr': 0, 'initial_lr': 0},
            {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
             'lr': 0, 'initial_lr': 0},
            {'params': net.layer_fuse.weight, 'weight_decay': weight_decay,
             'lr': learning_rate / 100, 'initial_lr': learning_rate / 100},
            {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100, 'initial_lr': 2 * learning_rate / 100},
        ], lr=learning_rate, momentum=momentum)

        if self.variant_offline is None:
            optimizer = default_var
        else:
            v = self.variant_offline
            params = [net.layer_stages.parameters, net.side_prep.parameters, net.score_dsn.parameters,
                      net.upscale_side_prep.parameters, net.upscale_score_dsn.parameters, net.layer_fuse.parameters]
            log.info('Offline variant: {0}'.format(v))
            if v == 0:
                optimizer = default_var
            elif v == 1:
                optimizer = optim.SGD(params)
            elif v == 2:
                optimizer = optim.Adam(params)
            elif v == 3:
                optimizer = optim.Adam([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': learning_rate / 10, 'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate / 10, 'initial_lr': 2 * learning_rate / 10},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': net.layer_fuse.weight, 'weight_decay': weight_decay,
                     'lr': learning_rate / 100, 'initial_lr': learning_rate / 100},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100,
                     'initial_lr': 2 * learning_rate / 100},
                ], lr=learning_rate)
            elif v == 4:
                optimizer = optim.Adagrad(params)
            elif v == 5:
                optimizer = optim.Adagrad([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': learning_rate / 10, 'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate / 10, 'initial_lr': 2 * learning_rate / 10},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': net.layer_fuse.weight, 'weight_decay': weight_decay,
                     'lr': learning_rate / 100, 'initial_lr': learning_rate / 100},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100,
                     'initial_lr': 2 * learning_rate / 100},
                ], lr=learning_rate)
            elif v == 6:
                optimizer = optim.Adadelta(params)
            elif v == 7:
                optimizer = optim.Adadelta([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': learning_rate / 10, 'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate / 10, 'initial_lr': 2 * learning_rate / 10},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': net.layer_fuse.weight, 'weight_decay': weight_decay,
                     'lr': learning_rate / 100, 'initial_lr': learning_rate / 100},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100,
                     'initial_lr': 2 * learning_rate / 100},
                ], lr=learning_rate)
            elif v == 8:
                optimizer = optim.Adamax(params)
            elif v == 9:
                optimizer = optim.Adamax([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay, 'initial_lr': learning_rate},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': learning_rate / 10, 'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
                    {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
                     'lr': 2 * learning_rate / 10, 'initial_lr': 2 * learning_rate / 10},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0, 'initial_lr': 0},
                    {'params': net.layer_fuse.weight, 'weight_decay': weight_decay,
                     'lr': learning_rate / 100, 'initial_lr': learning_rate / 100},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100,
                     'initial_lr': 2 * learning_rate / 100},
                ], lr=learning_rate)
            elif v == 10:
                optimizer = optim.Adam(net.parameters(), lr=1e-3, weight_decay=0.0002)
            elif v == 11:
                optimizer = optim.Adam(net.parameters(), lr=1e-4, weight_decay=0.0002)
            elif v == 12:
                optimizer = optim.Adam(net.parameters(), lr=1e-5, weight_decay=0.0002)
            elif v == 13:
                optimizer = optim.Adam(net.parameters(), lr=1e-6, weight_decay=0.0002)
            elif v == 14:
                optimizer = optim.Adam(net.parameters(), lr=1e-7, weight_decay=0.0002)
            elif v == 15:
                optimizer = optim.Adam(net.parameters(), lr=1e-8, weight_decay=0.0002)
            elif v == 16:
                optimizer = optim.SGD(net.parameters(), lr=1e-3, weight_decay=0.0002, momentum=0.9)
            elif v == 17:
                optimizer = optim.SGD(net.parameters(), lr=1e-4, weight_decay=0.0002, momentum=0.9)
            elif v == 18:
                optimizer = optim.SGD(net.parameters(), lr=1e-5, weight_decay=0.0002, momentum=0.9)
            elif v == 19:
                optimizer = optim.SGD(net.parameters(), lr=1e-6, weight_decay=0.0002, momentum=0.9)
            elif v == 20:
                optimizer = optim.SGD(net.parameters(), lr=1e-7, weight_decay=0.0002, momentum=0.9)
            elif v == 21:
                optimizer = optim.SGD(net.parameters(), lr=1e-8, weight_decay=0.0002, momentum=0.9)
            elif v == 22:
                optimizer = optim.Adam(net.parameters(), lr=1, weight_decay=0.0002)
            elif v == 23:
                optimizer = optim.Adam(net.parameters(), lr=1e-1, weight_decay=0.0002)
            elif v == 24:
                optimizer = optim.Adam(net.parameters(), lr=1e-2, weight_decay=0.0002)
            elif v == 25:
                optimizer = optim.SGD(net.parameters(), lr=1, weight_decay=0.0002, momentum=0.9)
            elif v == 26:
                optimizer = optim.SGD(net.parameters(), lr=1e-1, weight_decay=0.0002, momentum=0.9)
            elif v == 27:
                optimizer = optim.SGD(net.parameters(), lr=1e-2, weight_decay=0.0002, momentum=0.9)
            elif v == 28:
                optimizer = optim.Adam(net.parameters(), lr=2.5e-5, weight_decay=0.0002)
            elif v == 29:
                optimizer = optim.Adam(net.parameters(), lr=5e-5, weight_decay=0.0002)
            elif v == 30:
                optimizer = optim.Adam(net.parameters(), lr=7.5e-5, weight_decay=0.0002)
            elif v == 31:
                optimizer = optim.SGD(net.parameters(), lr=2.5e-8, weight_decay=0.0002, momentum=0.9)
            elif v == 32:
                optimizer = optim.SGD(net.parameters(), lr=5e-8, weight_decay=0.0002, momentum=0.9)
            elif v == 33:
                optimizer = optim.SGD(net.parameters(), lr=7.5e-8, weight_decay=0.0002, momentum=0.9)
            else:
                raise ValueError('invalid variant')
        return optimizer


class ResNetOnlineProvider(NetworkProvider):

    def __init__(self, name: str, save_dir: Path, settings: OnlineSettings, variant_offline: Optional[int] = None,
                 variant_online: Optional[int] = None, version: int = 18):
        super(ResNetOnlineProvider, self).__init__(name=name, save_dir=save_dir, settings=settings,
                                                   network_type=OSVOS_RESNET, variant_offline=variant_offline,
                                                   variant_online=variant_online)
        self.version = version

    def load_network_train(self) -> None:
        self.init_network(pretrained=False, version=self.version)
        self.load_model(self._settings.offline_epoch)

    def load_network_test(self, sequence: Optional[str] = None) -> None:
        self.init_network(pretrained=False, version=self.version)
        self.load_model(self._settings.n_epochs, sequence=sequence)

    def get_optimizer(self, learning_rate: float = 1e-8, weight_decay: float = 0.0002,
                      momentum: float = 0.9) -> Optimizer:
        net = self.network
        default_var = optim.SGD([
            {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay, 'initial_lr': learning_rate},
            {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
             'weight_decay': weight_decay, 'initial_lr': learning_rate},
            {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate, 'initial_lr': 2 * learning_rate},
            {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'weight' in pr[0]],
             'lr': learning_rate / 10, 'weight_decay': weight_decay, 'initial_lr': learning_rate / 10},
            {'params': [pr[1] for pr in net.score_dsn.named_parameters() if 'bias' in pr[0]],
             'lr': 2 * learning_rate / 10, 'initial_lr': 2 * learning_rate / 10},
            {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
             'lr': 0, 'initial_lr': 0},
            {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
             'lr': 0, 'initial_lr': 0},
            {'params': net.layer_fuse.weight, 'weight_decay': weight_decay,
             'lr': learning_rate / 100, 'initial_lr': learning_rate / 100},
            {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100, 'initial_lr': 2 * learning_rate / 100},
        ], lr=learning_rate, momentum=momentum)

        if self.variant_online is None:
            optimizer = default_var
        else:
            v = self.variant_online
            params = [net.layer_stages.parameters, net.side_prep.parameters, net.score_dsn.parameters,
                      net.upscale_side_prep.parameters, net.upscale_score_dsn.parameters, net.layer_fuse.parameters]
            log.info('Online variant: {0}'.format(v))
            if v == 0:
                optimizer = default_var
            elif v == 1:
                optimizer = optim.SGD([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': learning_rate * 2},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': learning_rate * 2},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0},
                    {'params': net.layer_fuse.weight, 'lr': learning_rate / 100, 'weight_decay': weight_decay},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100},
                ], lr=learning_rate, momentum=momentum)
            elif v == 2:
                optimizer = optim.SGD(params)
            elif v == 3:
                optimizer = optim.Adam(params)
            elif v == 4:
                optimizer = optim.Adam([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': learning_rate * 2},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': learning_rate * 2},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0},
                    {'params': net.layer_fuse.weight, 'lr': learning_rate / 100, 'weight_decay': weight_decay},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100},
                ], lr=learning_rate)
            elif v == 5:
                optimizer = optim.Adadelta(params)
            elif v == 6:
                optimizer = optim.Adadelta([
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay},
                    {'params': [pr[1] for pr in net.layer_stages.named_parameters() if 'bias' in pr[0]],
                     'lr': learning_rate * 2},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'weight' in pr[0]],
                     'weight_decay': weight_decay},
                    {'params': [pr[1] for pr in net.side_prep.named_parameters() if 'bias' in pr[0]],
                     'lr': learning_rate * 2},
                    {'params': [pr[1] for pr in net.upscale_side_prep.named_parameters() if 'weight' in pr[0]],
                     'lr': 0},
                    {'params': [pr[1] for pr in net.upscale_score_dsn.named_parameters() if 'weight' in pr[0]],
                     'lr': 0},
                    {'params': net.layer_fuse.weight, 'lr': learning_rate / 100, 'weight_decay': weight_decay},
                    {'params': net.layer_fuse.bias, 'lr': 2 * learning_rate / 100},
                ], lr=learning_rate)
            elif v == 10:
                optimizer = optim.Adam(net.parameters(), lr=1e-3, weight_decay=0.0002)
            elif v == 11:
                optimizer = optim.Adam(net.parameters(), lr=1e-4, weight_decay=0.0002)
            elif v == 12:
                optimizer = optim.Adam(net.parameters(), lr=1e-5, weight_decay=0.0002)
            elif v == 13:
                optimizer = optim.Adam(net.parameters(), lr=1e-6, weight_decay=0.0002)
            elif v == 14:
                optimizer = optim.Adam(net.parameters(), lr=1e-7, weight_decay=0.0002)
            elif v == 15:
                optimizer = optim.Adam(net.parameters(), lr=1e-8, weight_decay=0.0002)
            elif v == 16:
                optimizer = optim.SGD(net.parameters(), lr=1e-3, weight_decay=0.0002, momentum=0.9)
            elif v == 17:
                optimizer = optim.SGD(net.parameters(), lr=1e-4, weight_decay=0.0002, momentum=0.9)
            elif v == 18:
                optimizer = optim.SGD(net.parameters(), lr=1e-5, weight_decay=0.0002, momentum=0.9)
            elif v == 19:
                optimizer = optim.SGD(net.parameters(), lr=1e-6, weight_decay=0.0002, momentum=0.9)
            elif v == 20:
                optimizer = optim.SGD(net.parameters(), lr=1e-7, weight_decay=0.0002, momentum=0.9)
            elif v == 21:
                optimizer = optim.SGD(net.parameters(), lr=1e-8, weight_decay=0.0002, momentum=0.9)
            elif v == 22:
                optimizer = optim.Adam(net.parameters(), lr=1, weight_decay=0.0002)
            elif v == 23:
                optimizer = optim.Adam(net.parameters(), lr=1e-1, weight_decay=0.0002)
            elif v == 24:
                optimizer = optim.Adam(net.parameters(), lr=1e-2, weight_decay=0.0002)
            elif v == 25:
                optimizer = optim.SGD(net.parameters(), lr=1, weight_decay=0.0002, momentum=0.9)
            elif v == 26:
                optimizer = optim.SGD(net.parameters(), lr=1e-1, weight_decay=0.0002, momentum=0.9)
            elif v == 27:
                optimizer = optim.SGD(net.parameters(), lr=1e-2, weight_decay=0.0002, momentum=0.9)
            elif v == 28:
                optimizer = optim.Adam(net.parameters(), lr=2.5e-5, weight_decay=0.0002)
            elif v == 29:
                optimizer = optim.Adam(net.parameters(), lr=5e-5, weight_decay=0.0002)
            elif v == 30:
                optimizer = optim.Adam(net.parameters(), lr=7.5e-5, weight_decay=0.0002)
            elif v == 31:
                optimizer = optim.SGD(net.parameters(), lr=2.5e-8, weight_decay=0.0002, momentum=0.9)
            elif v == 32:
                optimizer = optim.SGD(net.parameters(), lr=5e-8, weight_decay=0.0002, momentum=0.9)
            elif v == 33:
                optimizer = optim.SGD(net.parameters(), lr=7.5e-8, weight_decay=0.0002, momentum=0.9)
            else:
                raise ValueError('invalid variant')
        return optimizer


provider_mapping = {
    ('offline', 'vgg16'): VGGOfflineProvider,
    ('online', 'vgg16'): VGGOnlineProvider,
    ('offline', 'resnet18'): ResNetOfflineProvider,
    ('online', 'resnet18'): ResNetOnlineProvider,
    ('offline', 'resnet34'): ResNetOfflineProvider,
    ('online', 'resnet34'): ResNetOnlineProvider
}  # type: Dict[(str, str), Type[NetworkProvider]]
