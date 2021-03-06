import argparse
from typing import Optional


def _get_base_parser():
    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument('--gpu-id', default=None, type=int, help='The gpu id to use')

    parser.add_argument('--network', default='vgg16', type=str, choices=['vgg16', 'resnet18', 'resnet34'],
                        help='The network to use')

    parser.add_argument('--no-training', action='store_true',
                        help='True if the program should train the model, else False')

    parser.add_argument('--no-testing', action='store_true',
                        help='True if the program should test the model, else False')

    parser.add_argument('--variant-offline', default=None, type=int, help='version to try')

    parser.add_argument('--eval-speeds', action='store_true', help='evaluates the network speeds')

    return parser


def parse_args(is_online: bool) -> argparse.Namespace:
    parser = _get_base_parser()
    if is_online:
        parser.add_argument('-s', '--sequence-name', default=None, type=Optional[str])
        parser.add_argument('-sg', '--sequence-group', default=None, type=Optional[int])
        parser.add_argument('-sgs', '--sequence-group-size', default=None, type=Optional[int])
        parser.add_argument('--variant-online', default=None, type=int, help='version to try')

    args = parser.parse_args()

    args.is_training = not args.no_training
    args.is_testing = not args.no_testing

    return args
