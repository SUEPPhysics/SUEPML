import argparse
import os
import sys
import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.distributed as dist
import torch.multiprocessing as mp
import tqdm
import warnings
import yaml
from termcolor import cprint

from torch.autograd import Variable
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed.optim import DistributedOptimizer
from torch.cuda.amp import GradScaler, autocast
from tqdm import trange
from ssd.checkpoints import EarlyStopping
from ssd.layers.functions import PriorBox
from ssd.layers.modules import MultiBoxLoss
from ssd.layers.regularizers import FLOPRegularizer
from ssd.generator import CalorimeterJetDataset
from ssd.net import build_ssd
from ssd.qutils import get_delta, get_alpha, to_ternary
from utils import AverageMeter, IsValidFile, Plotting, get_data_loader, \
    set_logging
from torchsummary import summary


warnings.filterwarnings(
    action='ignore',
    category=UserWarning,
    module=r'.*'
)


def execute(rank,
            world_size,
            name,
            ternary,
            int8,
            dataset,
            output,
            training_pref,
            ssd_settings,
            net_channels,
            trained_model_path,
            flop_regularizer,
            verbose,
            disco_mode):

    setup(rank, world_size)

    if rank == 0:
        logname = '{}/{}.log'.format(output['model'], name)
        logger = set_logging('Train_SSD', logname, verbose)

    ssd_settings['n_classes'] += 1
    save_dir = output['plots'] + "/" + name + "/"
    plot = Plotting(save_dir=save_dir)
    
    # Initialize dataset
    train_loader = get_data_loader(dataset['train'][rank],
                                   training_pref['batch_size_train'],
                                   training_pref['workers'],
                                   ssd_settings['input_dimensions'],
                                   ssd_settings['object_size'],
                                   rank,
                                   flip_prob=0.5,
                                   shuffle=True,
                                   return_pt=True,
                                   return_ntracks=True)

    val_loader = get_data_loader(dataset['validation'][rank],
                                 training_pref['batch_size_validation'],
                                 training_pref['workers'],
                                 ssd_settings['input_dimensions'],
                                 ssd_settings['object_size'],
                                 rank,
                                 shuffle=False,
                                 return_pt=True,
                                 return_ntracks=True)

    # Build SSD network
    ssd_net = build_ssd(rank, ssd_settings, net_channels, int8=int8)
    ssd_net = nn.SyncBatchNorm.convert_sync_batchnorm(ssd_net).to(rank)
    if int8:
        ssd_net.qconfig = torch.quantization.get_default_qat_qconfig('fbgemm')
        torch.quantization.prepare_qat(ssd_net, inplace=True)
    if rank == 0:
        logger.debug('SSD architecture:\n{}'.format(str(ssd_net)))
        # logger.debug(summary(ssd_net, input_size=(1,ssd_settings['input_dimensions'][1],ssd_settings['input_dimensions'][2])))
    
    # Initialize weights
    if trained_model_path:
        ssd_net.load_weights(trained_model_path)
    else:
        ssd_net.mobilenet.apply(weights_init)
        ssd_net.loc.apply(weights_init)
        ssd_net.cnf.apply(weights_init)
        ssd_net.reg.apply(weights_init)

    # Data parallelization
    cudnn.benchmark = True
    net = DDP(ssd_net, device_ids=[rank])

    # Set training objective parameters
    optimizer = optim.SGD(net.parameters(),
                          lr=training_pref['learning_rate'],
                          momentum=training_pref['momentum'],
                          weight_decay=training_pref['weight_decay'])
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
                                               milestones=[20, 30, 50, 60,
                                                           70, 80, 90],
                                               gamma=0.5)
    if rank == 0:
        cp_es = EarlyStopping(patience=training_pref['patience'],
                              save_path='%s/%s.pth' % (output['model'], name))
    if flop_regularizer:
        regularizer = FLOPRegularizer(ssd_settings['input_dimensions'], rank,
                                      strength=training_pref['reg_strength'])
    priors = Variable(PriorBox().apply(
        {'min_dim': ssd_settings['input_dimensions'][1:],
         'feature_maps': ssd_settings['feature_maps'],
         'steps': ssd_settings['steps'],
         'size': ssd_settings['object_size']}, rank))
    criterion = MultiBoxLoss(rank,
                             priors,
                             ssd_settings['n_classes'],
                             min_overlap=ssd_settings['overlap_threshold'],
                             beta_disco=ssd_settings['beta_disco'] if disco_mode else 1.0)
    scaler = GradScaler()
    verobse = verbose and rank == 0
    train_loss, val_loss = torch.empty(3 + int(disco_mode), 0), torch.empty(3 + int(disco_mode), 0)
    train_metrics, val_metrics = torch.empty(2, 0), torch.empty(2, 0)
    train_boxmetrics, val_boxmetrics = torch.empty(4, 0), torch.empty(4, 0)

    disco = AverageMeter('Disco', ':1.5f')
    loc = AverageMeter('Loc.', ':1.5f')
    cls = AverageMeter('Class.', ':1.5f')
    reg = AverageMeter('Reg.', ':1.5f')
    
    boxPreSUEP = AverageMeter('Pre. SUEP', ':1.5f')
    boxPreQCD = AverageMeter('Pre. QCD', ':1.5f')
    boxRecSUEP = AverageMeter('Rec. SUEP', ':1.5f')
    boxRecQCD = AverageMeter('Rec. QCD', ':1.5f')
    eventPre = AverageMeter('Pre. SUEP', ':1.5f')
    eventRec = AverageMeter('Rec. SUEP', ':1.5f')

    for epoch in range(1, training_pref['max_epochs']+1):

        # Start model training
        if verbose:
            tr = trange(len(train_loader), file=sys.stdout)
        if int8 and epoch > 3:
            # Freeze quantizer parameters
            net.apply(torch.quantization.disable_observer)
        if int8 and epoch > 2:
            # Freeze batch norm mean and variance estimates
            net.apply(torch.nn.intrinsic.qat.freeze_bn_stats)

        disco.reset()
        loc.reset()
        cls.reset()
        reg.reset()
        boxPreSUEP.reset()
        boxPreQCD.reset()
        boxRecSUEP.reset()
        boxRecQCD.reset()
        eventPre.reset()
        eventRec.reset()
        net.train()

        # Ternarize weights
        if ternary:
            for m in net.modules():
                if is_first_or_last(m):
                    delta = get_delta(m.weight.data)
                    m.weight.delta = delta
                    m.weight.alpha = get_alpha(m.weight.data, delta)

        for batch_index, (images, targets, ntracks) in enumerate(train_loader):

            # Ternarize weights
            if ternary:
                for m in net.modules():
                    if is_first_or_last(m):
                        m.weight.org = m.weight.data.clone()
                        m.weight.data = to_ternary(m.weight.data,
                                                   m.weight.delta,
                                                   m.weight.alpha)

            if flop_regularizer:
                rflop = regularizer.get_regularization(ssd_net.mobilenet)
            else:
                rflop = torch.tensor(0.)

            if int8:
                outputs = net(images)
                l, c, r = criterion(outputs, targets)
                loss = l + c + r + rflop
            else:
                with autocast():
                    outputs = net(images)
                    
                    if disco_mode:
                        # disco_variable = ntracks
                        # FIXME: this assumes no tracks overlap on the same pixel,
                        # roughly true for 500x500 images, but can and should be improved.
                        # Furthermore, calculating is at this step is a waste of time (how much?)
                        # might be better to include this information during data generation
                     
                        l, c, r, d, boxMetrics, eventMetrics = criterion(outputs, targets, torch.cat(ntracks))
                        loss = l + c + r + d + rflop
                        disco.update(d)
                    else:
                        l, c, r, boxMetrics, eventMetrics = criterion(outputs, targets)
                        loss = l + c + r + rflop

            loc.update(l)
            cls.update(c)
            reg.update(r)
            boxPreSUEP.update(boxMetrics[0][1])
            boxPreQCD.update(boxMetrics[0][2])
            boxRecSUEP.update(boxMetrics[1][1])
            boxRecQCD.update(boxMetrics[1][2])
            eventPre.update(eventMetrics[0])
            eventRec.update(eventMetrics[1])
                
            scaler.scale(loss).backward()

            if ternary:
                for m in net.modules():
                    if is_first_or_last(m):
                        m.weight.data.copy_(m.weight.org)

            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad()

            if ternary:
                for m in net.modules():
                    if is_first_or_last(m):
                        m.weight.org.copy_(m.weight.data.clamp_(-1, 1))

            if disco_mode:
                info = 'Epoch {}, {}, {}, {}, {}, {}, {}'.format(epoch, loc, cls, reg, disco,
                                                                     eventPre, eventRec)
            else:
                info = 'Epoch {}, {}, {}, {}, {}, {}'.format(epoch, loc, cls, reg, 
                                                                     eventPre, eventRec)
            if verbose:
                tr.set_description(info)
                tr.update(1)

        if rank == 0:
            logger.debug(info)
            
        if disco_mode: tloss = torch.tensor([loc.avg, cls.avg, reg.avg, disco.avg]).unsqueeze(1)
        else: tloss = torch.tensor([loc.avg, cls.avg, reg.avg]).unsqueeze(1)
        tmetrics = torch.tensor([eventPre.avg, eventRec.avg]).unsqueeze(1)
        tboxmetrics = torch.tensor([boxPreSUEP.avg, boxPreQCD.avg, boxRecSUEP.avg, boxRecQCD.avg]).unsqueeze(1)
        train_loss = torch.cat((train_loss, tloss), 1)
        train_metrics = torch.cat((train_metrics, tmetrics),1)
        train_boxmetrics = torch.cat((train_boxmetrics, tboxmetrics),1)
        if verbose:
            tr.close()

        # Start model validation
        if verbose:
            tr = trange(len(val_loader), file=sys.stdout)

        disco.reset()
        loc.reset()
        cls.reset()
        reg.reset()
        boxPreSUEP.reset()
        boxPreQCD.reset()
        boxRecSUEP.reset()
        boxRecQCD.reset()
        eventPre.reset()
        eventRec.reset()
        net.eval()

        with torch.no_grad():

            # Ternarize weights
            if ternary:
                for m in net.modules():
                    if is_first_or_last(m):
                        m.weight.org = m.weight.data.clone()
                        m.weight.data = to_ternary(m.weight.data)

            for batch_index, (images, targets, ntracks) in enumerate(val_loader):
                outputs = net(images)
                
                if disco_mode:
                    # disco_variable = ntracks
                    # FIXME: this assumes no tracks overlap on the same pixel,
                    # roughly true for 500x500 images, but can and should be improved.
                    # Furthermore, calculating is at this step is a waste of time (how much?)
                    # might be better to include this information during data generation
                    l, c, r, d, boxMetricsVal, eventMetricsVal = criterion(outputs, targets, torch.cat(ntracks))
                    l, c, r, d = reduce_tensor(l.data, c.data, r.data, d.data)
                    disco.update(d)
                else:
                    l, c, r, boxMetricsVal, eventMetricsVal = criterion(outputs, targets)
                    l, c, r = reduce_tensor(l.data, c.data, r.data)
                
                loc.update(l)
                cls.update(c)
                reg.update(r)
                boxPreSUEP.update(boxMetrics[0][1])
                boxPreQCD.update(boxMetrics[0][2])
                boxRecSUEP.update(boxMetrics[1][1])
                boxRecQCD.update(boxMetrics[1][2])
                eventPre.update(eventMetricsVal[0])
                eventRec.update(eventMetricsVal[1])

                if disco_mode:
                    info = 'Validation, {}, {}, {}, {}, {}, {}'.format(loc, cls, reg, disco, eventPre, eventRec)
                else:
                    info = 'Validation, {}, {}, {}, {}, {}'.format(loc, cls, reg, eventPre, eventRec)
                if verbose:
                    tr.set_description(info)
                    tr.update(1)

            if rank == 0:
                logger.debug(info)
            if disco_mode: vloss = torch.tensor([loc.avg, cls.avg, reg.avg, disco.avg]).unsqueeze(1)
            else: vloss = torch.tensor([loc.avg, cls.avg, reg.avg]).unsqueeze(1)
            vmetrics = torch.tensor([eventPre.avg, eventRec.avg]).unsqueeze(1)
            vboxmetrics = torch.tensor([boxPreSUEP.avg, boxPreQCD.avg, boxRecSUEP.avg, boxRecQCD.avg]).unsqueeze(1)
            val_loss = torch.cat((val_loss, vloss), 1)
            val_metrics = torch.cat((val_metrics, vmetrics),1)
            val_boxmetrics = torch.cat((val_boxmetrics, vboxmetrics),1)
            if verbose:
                tr.close()

            keys = ['Localization', 'Classification', 'Regression']
            if disco_mode: keys.append('Disco')
            plot.draw_loss(train_loss.cpu().numpy(),
                           val_loss.cpu().numpy(),
                           name,
                           keys=keys)
            
            plot.draw_metrics(train_metrics,
                           val_metrics,
                           ["Precision", "Recall"],
                           "event_metrics_" + name)
            
            plot.draw_metrics(train_boxmetrics,
                           val_boxmetrics,
                           ["Precision SUEP", "Precision QCD", "Recall SUEP", "Recall QCD"],
                           "box_metrics_" + name)

            if rank == 0 and cp_es(vloss.sum(0) + rflop.cpu(), ssd_net):
                break

            dist.barrier()

            if ternary:
                for m in net.modules():
                    if is_first_or_last(m):
                        m.weight.org.copy_(m.weight.data)
        scheduler.step()
    cleanup()


def is_first_or_last(layer):
    return (isinstance(layer, nn.Conv2d)
            and layer.kernel_size == (3, 3)
            and layer.in_channels > 3
            and layer.out_channels > 4)


def reduce_tensor(loc, cls, reg, disco=None):
    loc, cls, reg = loc.clone(), cls.clone(), reg.clone()
    dist.all_reduce(loc)
    dist.all_reduce(cls)
    dist.all_reduce(reg)
    loc /= int(os.environ['WORLD_SIZE'])
    cls /= int(os.environ['WORLD_SIZE'])
    reg /= int(os.environ['WORLD_SIZE'])
    if disco is not None: 
        disco = disco.clone()
        dist.all_reduce(disco)
        disco /= int(os.environ['WORLD_SIZE'])
        return loc, cls, reg, disco
    return loc, cls, reg


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        init.xavier_uniform_(m.weight.data)


def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = '127.0.0.1'
    os.environ['MASTER_PORT'] = '11223'
    os.environ['WORLD_SIZE'] = str(world_size)
    os.environ['RANK'] = str(rank)
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


def cleanup():
    dist.destroy_process_group()


if __name__ == '__main__':

    parser = argparse.ArgumentParser('Train Single Shot Jet Detection Model')
    parser.add_argument('name', type=str, help='Model name')
    parser.add_argument('-c', '--config', action=IsValidFile, type=str,
                        help='Path to config file', default='ssd-config.yml')
    parser.add_argument('-s', '--structure', action=IsValidFile, type=str,
                        help='Path to config file', default='net-config.yml')
    parser.add_argument('-m', '--pre-trained-model', action=IsValidFile,
                        default=None, dest='pre_trained_model_path', type=str,
                        help='Path to pre-trained model')
    parser.add_argument('-8', '--int8', action='store_true',
                        help='Train int8 network')
    parser.add_argument('-t', '--ternary', action='store_true',
                        help='Ternarize weights')
    parser.add_argument('-r', '--flop-regularizer', action='store_true',
                        help='Run with FLOP regularizer')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Output verbosity')
    parser.add_argument('-d', '--disco-mode', action='store_true',
                        help='DISCO MODE')
    args = parser.parse_args()
    config = yaml.safe_load(open(args.config))
    net_config = yaml.safe_load(open(args.structure))

    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    world_size = torch.cuda.device_count()
    print("World size:", world_size)
    
    if args.disco_mode: cprint("𝅘𝅥𝅮𝅘𝅥𝅮𝅘𝅥𝅮 Disco Mode 𝅘𝅥𝅮𝅘𝅥𝅮𝅘𝅥𝅮", 'white', 'on_magenta')
    
    # create a directory to contain all the training plots with the name of the model
    save_dir = config['output']['plots'] + "/" + args.name + "/"
    if not os.path.isdir(save_dir): os.system("mkdir " + save_dir)
    
    # and copy the configuration file into it
    os.system("cp " + args.config + " " + save_dir)
    
    mp.spawn(execute,
             args=(world_size,
                   args.name,
                   args.ternary,
                   args.int8,
                   config['dataset'],
                   config['output'],
                   config['training_pref'],
                   config['ssd_settings'],
                   net_config['network_channels'],
                   args.pre_trained_model_path,
                   args.flop_regularizer,
                   args.verbose,
                   args.disco_mode),
             nprocs=world_size,
             join=True)
