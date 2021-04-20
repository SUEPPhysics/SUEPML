import argparse
import torch
import time
import yaml

from ssd.net import build_ssd
from utils import *

import tensorrt as trt
import pycuda.driver as cuda
import pycuda.autoinit


def to_numpy(t):
    return t.detach().cpu().numpy() if t.requires_grad else t.cpu().numpy()


if __name__ == '__main__':

    parser = argparse.ArgumentParser("Measure SSD's Inference Time")
    parser.add_argument('model', type=str, help='Input model name')
    parser.add_argument('-b', '--batchsize', type=int,
                        help='Batch size', default=1)
    parser.add_argument('-t', '--ternary', action='store_true',
                        help='Quasi-TWN optimization')
    parser.add_argument('-s', '--suppress', action='store_true',
                        help='Suppress checks')
    parser.add_argument('-c', '--config', action=IsValidFile, type=str,
                        help='Path to config file', default='ssd-config.yml')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Output verbosity')
    args = parser.parse_args()
    config = yaml.safe_load(open(args.config))

    base = '{}/{}'.format(config['output']['model'], args.model)
    source_path_torch = '{}.pth'.format(base)
    source_path_onnx = '{}.onnx'.format(base)
    logger = set_logging('Test_SSD', '{}.log'.format(base), args.verbose)
    logger.info('Testing {0} model'.format(source_path_onnx))

    ssd_settings = config['ssd_settings']
    ssd_settings['n_classes'] += 1
    in_dim = ssd_settings['input_dimensions']
    jet_size = ssd_settings['object_size']
    workers = config['evaluation_pref']['workers']
    samples = config['inference_pref']['samples']

    net = build_ssd('cpu', ssd_settings, inference=True, onnx=True)
    net.load_weights(source_path_torch)
    net.eval()
    loader = get_data_loader(config['dataset']['validation'][0],
                             args.batchsize,
                             workers,
                             in_dim,
                             jet_size,
                             shuffle=False)
    batch_iterator = iter(loader)
    batch, _ = next(batch_iterator)
    batch = batch.cpu()

    lgr = trt.Logger(trt.Logger.INFO)
    net_flag = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)

    with trt.Builder(lgr) as builder, \
        builder.create_network(net_flag) as network, \
        trt.OnnxParser(network, lgr) as parser, \
        builder.create_builder_config() as cfg:

        builder.max_batch_size = args.batchsize
        builder.max_workspace_size = 1 << 30

        logger.info('Parsing ONNX file')
        with open(source_path_onnx, 'rb') as model:
            if not parser.parse(model.read()):
                raise RuntimeError('Parsing failed! Error: {:}'.format(
                    parser.get_error(0).desc()))

        size = (args.batchsize, *in_dim)
        profile = builder.create_optimization_profile()
        profile.set_shape("input", size, size, size)
        cfg.add_optimization_profile(profile)

        if args.ternary:
            cfg.set_flag(trt.BuilderFlag.FP16)
            cfg.set_flag(trt.BuilderFlag.STRICT_TYPES)

        logger.info('Building TensorRT engine')
        engine = builder.build_engine(network, cfg)

        host_in, cuda_in, host_out, cuda_out, bindings = [], [], [], [], []
        stream = cuda.Stream()

        for binding in engine:
            size = trt.volume(engine.get_binding_shape(binding))
            host_mem = cuda.pagelocked_empty(size, dtype=np.float32)
            cuda_mem = cuda.mem_alloc(host_mem.nbytes)
            bindings.append(int(cuda_mem))

            if engine.binding_is_input(binding):
                host_in.append(host_mem)
                cuda_in.append(cuda_mem)
            else:
                host_out.append(host_mem)
                cuda_out.append(cuda_mem)
                context = engine.create_execution_context()

        np.copyto(host_in[0], batch.numpy().ravel())

        logger.info('GPU warm-up')
        for _ in range(10):
            cuda.memcpy_htod_async(cuda_in[0], host_in[0], stream)
            context.execute_async(batch_size=args.batchsize,
                                  bindings=bindings,
                                  stream_handle=stream.handle)

        logger.info('Measuring latency')
        measurements = 0
        for i in range(samples):
            t_start = time.time()
            cuda.memcpy_htod_async(cuda_in[0], host_in[0], stream)
            context.execute_async(batch_size=args.batchsize,
                                  bindings=bindings,
                                  stream_handle=stream.handle)
            cuda.memcpy_dtoh_async(host_out[2], cuda_out[2], stream)
            cuda.memcpy_dtoh_async(host_out[1], cuda_out[1], stream)
            cuda.memcpy_dtoh_async(host_out[0], cuda_out[0], stream)
            stream.synchronize()
            elapsed_time = time.time() - t_start
            measurements += elapsed_time * 1e6

        latency = measurements / samples
        throughput = 1e6 * args.batchsize / latency

        logger.info('Batch size {0}'.format(args.batchsize))
        logger.info('Latency: {0:.2f} us'.format(latency))
        logger.info('Throughput: {0:.2f} eps'.format(throughput))

        if not args.suppress:
            t = list(map(to_numpy, list(net(batch))))
            for i, task in enumerate(['Localization',
                                      'Classification',
                                      'Regression']):
                np.testing.assert_allclose(t[i],
                                           host_out[i].reshape(t[i].shape),
                                           rtol=1e-03,
                                           atol=1e-05)
                logger.info('{} task: OK'.format(task))