import sys
import timeit
from pathlib import Path

from tensorboardX import SummaryWriter
from torch import optim, nn
from torch.autograd import Variable
from torch.utils.data import DataLoader

from config.mypath import Path as P
from layers.osvos_layers import class_balanced_cross_entropy_loss
from util import gpu_handler, io_helper, experiment_helper, args_helper
from util.logger import get_logger
from util.network_provider import NetworkProvider, provider_mapping
from util.settings import OnlineSettings

if P.is_custom_pytorch():
    sys.path.append(P.custom_pytorch())  # Custom PyTorch

log = get_logger(__file__)


def train_and_test(net_provider: NetworkProvider, seq_name: str, settings: OnlineSettings) -> None:
    io_helper.write_settings(save_dir_models, net_provider.name, settings, variant_offline=settings.variant_offline,
                             variant_online=settings.variant_online)
    summary_writer = _get_summary_writer(path_stem)

    if settings.is_training:
        net_provider.load_network_train()
        data_loader = io_helper.get_data_loader_train(db_root_dir, settings.batch_size_train, seq_name)
        optimizer = net_provider.get_optimizer()

        _train(net_provider, data_loader, optimizer, summary_writer, seq_name, settings.start_epoch, settings.n_epochs,
               settings.avg_grad_every_n, settings.snapshot_every_n)

    if settings.is_testing:
        net_provider.load_network_test(sequence=seq_name)
        data_loader = io_helper.get_data_loader_test(db_root_dir, settings.batch_size_test, seq_name)

        if settings.variant_offline is None:
            save_dir = save_dir_results / net_provider.name / 'online'
        else:
            save_dir = (save_dir_results / net_provider.name / str(settings.variant_offline) /
                        str(settings.variant_online))

        experiment_helper.test(net_provider, data_loader, save_dir, settings.is_visualizing_results,
                               settings.eval_speeds, seq_name=seq_name)

    if settings.is_visualizing_network:
        io_helper.visualize_network(net_provider.network)


def _get_summary_writer(seq_name: str) -> SummaryWriter:
    path_tensorboard = Path('tensorboard') / path_stem
    return io_helper.get_summary_writer(path_tensorboard)


def _train(net_provider: NetworkProvider, dataloader: DataLoader, optimizer: optim.SGD, summary_writer: SummaryWriter,
           seq_name: str, start_epoch: int, n_epochs: int, avg_grad_every_n: int, snapshot_every_n: int) -> None:
    log.info('Start of Online Training, sequence: ' + seq_name)

    net = net_provider.network

    speeds_training = []
    n_samples = len(dataloader)
    loss_tr = []
    counter_gradient = 0

    time_all_start = timeit.default_timer()
    for epoch in range(start_epoch, n_epochs):
        time_epoch_start = timeit.default_timer()

        running_loss_tr = 0
        loss_epoch = 0.0
        for minibatch_index, minibatch in enumerate(dataloader):
            inputs, gts = minibatch['image'], minibatch['gt']
            inputs, gts = gpu_handler.cast_cuda_if_possible([inputs, gts])

            outputs = net.forward(inputs)

            loss = class_balanced_cross_entropy_loss(outputs[-1], gts, size_average=False)
            running_loss_tr += loss.data[0]

            if epoch % (n_epochs // 20) == (n_epochs // 20 - 1):
                running_loss_tr /= n_samples
                loss_tr.append(running_loss_tr)

                log.info('[Epoch {0}: {1}, numImages: {2}]'.format(seq_name, epoch + 1, minibatch_index + 1))
                log.info('Loss {0}: {1}'.format(seq_name, running_loss_tr))
                summary_writer.add_scalar('data/total_loss_epoch', running_loss_tr, epoch)

            loss /= avg_grad_every_n
            loss.backward()
            loss_epoch += loss.item()
            counter_gradient += 1

            if counter_gradient % avg_grad_every_n == 0:
                summary_writer.add_scalar('data/total_loss_iter', loss.data[0], minibatch_index + n_samples * epoch)
                optimizer.step()
                optimizer.zero_grad()
                counter_gradient = 0

        loss_epoch /= len(dataloader.dataset)
        summary_writer.add_scalar('data/{mode}/loss'.format(mode='train'), loss_epoch, epoch)

        if (epoch % snapshot_every_n) == snapshot_every_n - 1:  # and epoch != 0:
            net_provider.save_model(epoch, sequence=seq_name)

        time_epoch_stop = timeit.default_timer()
        time_for_epoch = time_epoch_stop - time_epoch_start
        speeds_training.append(time_for_epoch)

    time_all_stop = timeit.default_timer()
    time_for_all = time_all_stop - time_all_start
    n_images = len(dataloader)
    time_per_sample = time_for_all / n_images
    log.info('Train {0}: total time {1} sec'.format(seq_name, str(time_for_all)))
    log.info('Train {0}: {1} images'.format(seq_name, str(n_images)))
    log.info('Train {0}: time per sample {1} sec'.format(seq_name, str(time_per_sample)))


if __name__ == '__main__':
    from playground.pruning_resource_efficient_inference import BasicBlockDummy

    args = args_helper.parse_args(is_online=True)
    gpu_handler.select_gpu(args.gpu_id)

    db_root_dir = P.db_root_dir()
    exp_dir = P.exp_dir()

    save_dir_models = Path('models')
    save_dir_models.mkdir(parents=True, exist_ok=True)
    save_dir_results = Path('results')
    save_dir_results.mkdir(parents=True, exist_ok=True)

    path_stem = 'resnet18/11'
    path_stem += '/' + 'prune'
    path_stem += '/' + 'exp'
    path_stem += '/' + 'offline'
    path_input_model = Path('models') / path_stem / 'percentage.pth'
    path_stem += '/' + 'online'
    log.info('Path stem: %s', str(path_stem))

    path_output_model_base = Path('models') / path_stem
    path_output_model_base.mkdir(parents=True, exist_ok=True)

    settings = OnlineSettings(is_training=args.is_training, is_testing=args.is_testing, start_epoch=0, n_epochs=10000,
                              avg_grad_every_n=5, snapshot_every_n=10000, is_testing_while_training=False,
                              test_every_n=5, batch_size_train=1, batch_size_test=1, is_visualizing_network=False,
                              is_visualizing_results=False, offline_epoch=240,
                              variant_offline=args.variant_offline, variant_online=args.variant_online,
                              eval_speeds=args.eval_speeds)

    provider_class = provider_mapping[('online', args.network)]
    if args.network == 'resnet34':
        net_provider = provider_class(name=args.network, save_dir=(path_input_model, path_output_model_base),
                                      settings=settings,
                                      variant_offline=args.variant_offline, variant_online=args.variant_online,
                                      version=34)
    else:
        net_provider = provider_class(name=args.network, save_dir=(path_input_model, path_output_model_base),
                                      settings=settings,
                                      variant_offline=args.variant_offline, variant_online=args.variant_online)

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

        [train_and_test(net_provider, s, settings)
         for s in sequences]

    else:
        train_and_test(net_provider, args.sequence_name, settings)
