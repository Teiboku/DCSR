import os
import math
from decimal import Decimal

import utility
import torch.nn.functional as F
import torch
import torch.nn as nn
import torch.nn.utils as utils
from tqdm import tqdm
import imageio
class Trainer():
    def __init__(self, args, loader, my_model, my_loss, ckp):
        self.args = args
        self.scale = args.scale

        # Add DDP setup
        if torch.cuda.device_count() > 1:
            print(f"Using {torch.cuda.device_count()} GPUs!")
            self.model = nn.DataParallel(my_model)
        else:
            self.model = my_model

        self.ckp = ckp
        self.loader_train = loader.loader_train
        self.loader_test = loader.loader_test
        self.loss = my_loss
        self.optimizer = utility.make_optimizer(args, self.model)

        if self.args.load != '':
            self.optimizer.load(ckp.dir, epoch=len(ckp.log))

        self.error_last = 1e8

    def train(self):
        self.loss.step()
        epoch = self.optimizer.get_last_epoch() + 1
        lr = self.optimizer.get_lr()

        self.ckp.write_log(
            '[Epoch {}]\tLearning rate: {:.2e}'.format(epoch, Decimal(lr))
        )
        self.loss.start_log()
        self.model.train()

        timer_data, timer_model = utility.timer(), utility.timer()

        for batch, (lr, hr, ref, _) in enumerate(self.loader_train):

            lr, hr, ref = self.prepare(lr, hr, ref)
            timer_data.hold()
            timer_model.tic()

            self.optimizer.zero_grad()
            # Handle multi-GPU
            if isinstance(self.model, nn.DataParallel):
                sr = self.model.module(lr, ref)
            else:
                sr = self.model(lr, ref)
            loss = self.loss(sr, hr, ref)
                       
            
            loss.backward()
            if self.args.gclip > 0:
                utils.clip_grad_value_(
                    self.model.parameters(),
                    self.args.gclip
                )
            self.optimizer.step()
            timer_model.hold()

            if (batch + 1) % self.args.print_every == 0:
                self.ckp.write_log('[{}/{}]\t{}\t{:.1f}+{:.1f}s'.format(
                    (batch + 1) * self.args.batch_size,
                    len(self.loader_train.dataset),
                    self.loss.display_loss(batch),
                    timer_model.release(),
                    timer_data.release()))

            timer_data.tic()

        self.loss.end_log(len(self.loader_train))
        self.error_last = self.loss.log[-1, -1]
        self.optimizer.schedule()

    def test(self):
        torch.set_grad_enabled(False)

        epoch = self.optimizer.get_last_epoch()
        self.ckp.write_log('\nEvaluation:')
        self.ckp.add_log(
            torch.zeros(1, len(self.loader_test), 1)
        )
        self.model.eval()

        timer_test = utility.timer()
        if self.args.save_results: self.ckp.begin_background()
        for idx_data, d in enumerate(self.loader_test):
            scale = 2

            for lr, hr, ref, filename in tqdm(d, ncols=80):

                lr, hr, ref = self.prepare(lr, hr, ref)

                # Handle multi-GPU
                if isinstance(self.model, nn.DataParallel):
                    sr = self.model.module(lr, ref)
                else:
                    sr = self.model(lr, ref)

                sr = utility.quantize(sr, self.args.rgb_range)

                save_list = [sr]
                self.ckp.log[-1, idx_data, 0] += utility.calc_psnr(
                    sr, hr, scale, self.args.rgb_range, dataset=d
                ) 
                if self.args.save_gt:
                    save_list.extend([lr, hr])

                if self.args.save_results:
                    self.ckp.save_results(filename[0], save_list, scale)

            self.ckp.log[-1, idx_data, 0] /= len(d)
            self.ckp.write_log(
                '[{} x{}]\tPSNR: {:.3f}'.format(
                    d.dataset.name,
                    scale,
                    self.ckp.log[-1, idx_data, 0]
                )
            )

        self.ckp.write_log('Forward: {:.2f}s\n'.format(timer_test.toc()))
        self.ckp.write_log('Saving...')

        if self.args.save_results:
            self.ckp.end_background()

        if not self.args.test_only:
            self.ckp.save(self, epoch)

        self.ckp.write_log(
            'Total: {:.2f}s\n'.format(timer_test.toc()), refresh=True
        )

        torch.set_grad_enabled(True)

    def prepare(self, *args):
        device = torch.device('cpu' if self.args.cpu else 'cuda')

        def _prepare(tensor):
            if self.args.precision == 'half': tensor = tensor.half()
            return tensor.to(device)

        return [_prepare(a) for a in args]


