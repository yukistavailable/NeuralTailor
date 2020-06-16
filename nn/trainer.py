# Training loop func
import numpy as np
import os
import time

import torch
import torch.nn as nn
import wandb as wb

class Trainer():
    def __init__(self, data_name, project_name='Train', run_name='Run', no_sync=False):
        """Initialize training"""
        self.project = project_name
        self.run_name = run_name
        # default training setup
        self.setup = dict(
            random_seed=int(time.time()),
            dataset=data_name,
            device='cuda:0' if torch.cuda.is_available() else 'cpu',
            epochs=100,
            batch_size=64,
            learning_rate=0.001,
            loss='MSELoss',
            optimizer='SGD',
            lr_scheduling=True
        )

        self.no_sync = no_sync
   
    def init_randomizer(self):
        """Init randomizatoin for torch globally for reproducibility"""
        # see https://pytorch.org/docs/stable/notes/randomness.html
        torch.manual_seed(self.setup['random_seed'])
        if 'cuda' in wb.config.device:
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

    def fit(self, model, train_loader, valid_loader, run_name=''):

        if run_name:
            self.run_name = run_name
        self.setup['model'] = model.__class__.__name__

        self._add_optimizer(model)
        self._add_loss()
        self._add_scheduler()

        self._init_wb_run(model)

        self.device = torch.device(wb.config.device)
        print('NN training Using device: {}'.format(self.device))
        
        self._fit_loop(model, train_loader, valid_loader)
        print ("Finished training")

    def update_config(self, **kwargs):
        """add given values to training config"""
        self.setup.update(kwargs)

    # ---- Private -----
    def _init_wb_run(self, model):
        # init Weights&biases run
        if self.no_sync:
            os.environ['WANDB_MODE'] = 'dryrun'  # No sync with cloud
        wb.init(name=self.run_name, project=self.project, config=self.setup)
        wb.watch(model, log='all')

    def _add_optimizer(self, model):
        
        if self.setup['optimizer'] == 'SGD':
            # future 'else'
            print('NN Warning::Using default SGD optimizer')
            self.optimizer = torch.optim.SGD(model.parameters(), lr = wb.config.learning_rate)
        
    def _add_loss(self):
        if self.setup['loss'] == 'MSELoss':
            # future 'else'
            print('NN Warning::Using default MSELoss loss')
            self.regression_loss = nn.MSELoss()

    def _add_scheduler(self):
        if ('lr_scheduling' in self.setup
                and self.setup['lr_scheduling']):
            self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min', factor=0.1, patience=1)
        else:
            print('NN Warning: no learning scheduling set')

    def _fit_loop(self, model, train_loader, valid_loader):
        """Fit loop with the setup already performed"""
        model.to(self.device)
        for epoch in range (wb.config.epochs):
            model.train()
            for i, batch in enumerate(train_loader):
                features, params = batch['features'].to(self.device), batch['pattern_params'].to(self.device)
                
                #with torch.autograd.detect_anomaly():
                preds = model(features)
                loss = self.regression_loss(preds, params)
                #print ('Epoch: {}, Batch: {}, Loss: {}'.format(epoch, i, loss))
                loss.backward()
                
                self.optimizer.step()
                self.optimizer.zero_grad()
                
                # logging
                if i % 5 == 4:
                    wb.log({'epoch': epoch, 'loss': loss})
            
            model.eval()
            with torch.no_grad():
                losses, nums = zip(
                    *[(self.regression_loss(model(features), params), len(batch)) for batch in valid_loader]
                )
                
            valid_loss = np.sum(losses) / np.sum(nums)
            self.scheduler.step(valid_loss)
            
            print ('Epoch: {}, Validation Loss: {}'.format(epoch, valid_loss))
            wb.log({'epoch': epoch, 'valid_loss': valid_loss, 'learning_rate': self.optimizer.param_groups[0]['lr']})
