import torch
import torch.nn as nn
from sparsemax import Sparsemax

# my modules
from nn.metrics.composed_loss import ComposedLoss, ComposedPatternLoss
import nn.net_blocks as blocks


# ------ Basic Interface --------
class BaseModule(nn.Module):
    """Base interface for my neural nets"""
    def __init__(self):
        super().__init__()
        self.config = {
            'loss': 'MSELoss',
            'model': self.__class__.__name__
        }
        self.regression_loss = nn.MSELoss()
    
    def loss(self, preds, ground_truth, **kwargs):
        """Default loss for my neural networks. Takes pne batch of data. 
            Children can use additional arguments as needed
        """
        ground_truth = ground_truth.to(preds.device)  # make sure device is correct
        loss = self.regression_loss(preds, ground_truth)
        return loss, {'regression loss': loss}, False  # second term is for compound losses, third -- to indicate dynamic update of loss structure

    def train(self, mode=True):
        super().train(mode)
        if isinstance(self.loss, object):
            self.loss.train(mode)
    
    def eval(self):
        super().eval()
        if isinstance(self.loss, object):
            self.loss.eval()


# ------------ Pattern predictions ----------
class GarmentFullPattern3D(BaseModule):
    """
        Predicting 2D pattern inluding panel placement and stitches information from 3D garment geometry 
        Constists of 
            * (interchangeable) feature extractor 
            * pattern decoder from GarmentPatternAE
            * MLP modules to predict panel 3D placement & stitches
    """
    def __init__(self, data_config, config={}, in_loss_config={}):
        super().__init__()

        # output props
        self.panel_elem_len = data_config['element_size']
        self.max_panel_len = data_config['max_panel_len']
        self.max_pattern_size = data_config['max_pattern_len']
        self.rotation_size = data_config['rotation_size']
        self.translation_size = data_config['translation_size']

        # ---- Net configuration ----
        self.config.update({
            'panel_encoding_size': 250, 
            'panel_hidden_size': 250,
            'panel_n_layers': 3, 
            'pattern_encoding_size': 250, 
            'pattern_hidden_size': 250, 
            'pattern_n_layers': 2, 
            'dropout': 0,
            'lstm_init': 'kaiming_normal_', 
            'feature_extractor': 'EdgeConvFeatures',
            'panel_decoder': 'LSTMDecoderModule', 
            'pattern_decoder': 'LSTMDecoderModule', 
            'stitch_tag_dim': 3
        })
        # Adjust input settings for backwards compatibility (older runs had these parameters together)
        if 'panel_hidden_size' not in config:
            config['panel_hidden_size'] = config['panel_encoding_size']
        if 'pattern_hidden_size' not in config:
            config['pattern_hidden_size'] = config['pattern_encoding_size']
        # update with input settings
        self.config.update(config) 

        # ---- losses configuration ----
        self.config['loss'] = {
            'loss_components': ['shape', 'loop', 'rotation', 'translation'],  # , 'stitch', 'free_class'],
            'quality_components': ['shape', 'discrete', 'rotation', 'translation'],  #, 'stitch', 'free_class'],
            'panel_origin_invariant_loss': False,
            'loop_loss_weight': 1.,
            'stitch_tags_margin': 0.3,
            'epoch_with_stitches': 40, 
            'stitch_supervised_weight': 0.1,   # only used when explicit stitch loss is used
            'stitch_hardnet_version': False,
            'panel_origin_invariant_loss': True
        }
        self.config['loss'].update(in_loss_config)
        # loss object
        self.loss = ComposedPatternLoss(data_config, self.config['loss'])
        self.config['loss'] = self.loss.config  # sync just in case

        # ---- Feature extractor definition -------
        feature_extractor_module = getattr(blocks, self.config['feature_extractor'])
        self.feature_extractor = feature_extractor_module(self.config['pattern_encoding_size'], self.config)
        if hasattr(self.feature_extractor, 'config'):
            self.config.update(self.feature_extractor.config)   # save extractor's additional configuration

        # ----- Decode into pattern definition -------
        panel_decoder_module = getattr(blocks, self.config['panel_decoder'])
        self.panel_decoder = panel_decoder_module(
            encoding_size=self.config['panel_encoding_size'], 
            hidden_size=self.config['panel_hidden_size'], 
            out_elem_size=self.panel_elem_len + self.config['stitch_tag_dim'] + 1,  # last element is free tag indicator 
            n_layers=self.config['panel_n_layers'], 
            out_len = self.max_panel_len,
            dropout=self.config['dropout'], 
            custom_init=self.config['lstm_init']
        )
        pattern_decoder_module = getattr(blocks, self.config['pattern_decoder'])
        self.pattern_decoder = pattern_decoder_module(
            encoding_size=self.config['pattern_encoding_size'], 
            hidden_size=self.config['pattern_hidden_size'], 
            out_elem_size=self.config['panel_encoding_size'], 
            n_layers=self.config['pattern_n_layers'], 
            out_len=self.max_pattern_size,
            dropout=self.config['dropout'],
            custom_init=self.config['lstm_init']
        )

        # decoding the panel placement
        self.placement_decoder = nn.Linear(
            self.config['panel_encoding_size'], 
            self.rotation_size + self.translation_size)

    def forward_encode(self, positions_batch):
        """
            Predict garment encodings for input point coulds batch
        """
        return self.feature_extractor(positions_batch)[0]  # YAAAAY Pattern hidden representation!!

    def forward_pattern_decode(self, garment_encodings):
        """
            Unfold provided garment encodings into per-panel encodings
            Useful for obtaining the latent space for Panels
        """
        panel_encodings = self.pattern_decoder(garment_encodings, self.max_pattern_size)
        flat_panel_encodings = panel_encodings.contiguous().view(-1, panel_encodings.shape[-1])

        return flat_panel_encodings

    def forward_panel_decode(self, flat_panel_encodings, batch_size):
        """ Panel encodings to outlines & stitch info """
        flat_panels = self.panel_decoder(flat_panel_encodings, self.max_panel_len)
        
        # Placement
        flat_placement = self.placement_decoder(flat_panel_encodings)
        flat_rotations = flat_placement[:, :self.rotation_size]
        flat_translations = flat_placement[:, self.rotation_size:]

        # reshape back to per-pattern predictions
        panel_predictions = flat_panels.contiguous().view(batch_size, self.max_pattern_size, self.max_panel_len, -1)
        stitch_tags = panel_predictions[:, :, :, self.panel_elem_len:-1]
        free_edge_class = panel_predictions[:, :, :, -1]
        outlines = panel_predictions[:, :, :, :self.panel_elem_len]

        rotations = flat_rotations.contiguous().view(batch_size, self.max_pattern_size, -1)
        translations = flat_translations.contiguous().view(batch_size, self.max_pattern_size, -1)

        return {
            'outlines': outlines, 
            'rotations': rotations, 'translations': translations, 
            'stitch_tags': stitch_tags, 'free_edges_mask': free_edge_class}

    def forward_decode(self, garment_encodings):
        """
            Unfold provided garment encodings into the sewing pattens
        """
        flat_panel_encodings = self.forward_pattern_decode(garment_encodings)

        return self.forward_panel_decode(flat_panel_encodings, garment_encodings.size(0))

    def forward(self, positions_batch, **kwargs):
        # Extract info from geometry 
        pattern_encodings = self.forward_encode(positions_batch)

        # Decode 
        return self.forward_decode(pattern_encodings)


class GarmentSegmentPattern3D(GarmentFullPattern3D):
    """
        Patterns from 3D data with point-level attention.
        Forward functions are subdivided for convenience of latent space inspection
    """
    def __init__(self, data_config, config={}, in_loss_config={}):

        if 'loss_components' not in in_loss_config:
            # 'stitch', 'free_class' for enabling stitch prediction
            in_loss_config.update(
                loss_components=['shape', 'loop', 'rotation', 'translation'], 
                quality_components=['shape', 'discrete', 'rotation', 'translation']
            )

        super().__init__(data_config, config, in_loss_config)

        # set to true to get attention weights with prediction -- for visualization or loss evaluation
        # Keep false in all unnecessary cases to save memory!
        self.save_att_weights = 'segmentation' in self.loss.config['loss_components']

        # defaults
        if 'local_attention' not in self.config:
            # Has to be false for the old runs that don't have this setting and rely on global attention
            self.config['local_attention'] = False  

        # ---- per-point attention module ---- 
        # that performs sort of segmentation
        # taking in per-point features and global encoding, outputting point weight per (potential) panel
        # Segmentaition aims to ensure that each point belongs to min number of panels
        # Global context gives understanding of the cutting pattern 
        attention_input_size = self.feature_extractor.config['EConv_feature']  
        if not self.config['local_attention']:  # adding global  feature
            attention_input_size += self.config['pattern_encoding_size']
        if self.config['skip_connections']:
            attention_input_size += 3  # initial coordinates

        self.point_segment_mlp = nn.Sequential(
            blocks.MLP([attention_input_size, attention_input_size, attention_input_size, self.max_pattern_size]),
            Sparsemax(dim=1)  # in the feature dimention
        )

        # additional panel encoding post-procedding
        panel_att_out_size = self.feature_extractor.config['EConv_feature']
        if self.config['skip_connections']: 
            panel_att_out_size += 3
        self.panel_dec_lin = nn.Linear(
            panel_att_out_size, self.feature_extractor.config['panel_encoding_size'])

        # pattern decoder is not needed in this acrchitecture
        del self.pattern_decoder

    def forward_panel_enc_from_3d(self, positions_batch):
        """
            Get per-panel encodings from 3D data directly
            
        """
        # ------ Point cloud features -------
        batch_size = positions_batch.shape[0]
        # per-point and total encodings
        init_pattern_encodings, point_features_flat, batch = self.feature_extractor(
            positions_batch, 
            not self.config['local_attention']  # don't need global pool in this case
        )
        num_points = point_features_flat.shape[0] // batch_size

        # ----- Predict per-point panel scores (as attention weights) -----
        # propagate the per-pattern global encoding for each point
        if self.config['local_attention']:
            points_weights = self.point_segment_mlp(point_features_flat)
        else:
            global_enc_propagated = init_pattern_encodings.unsqueeze(1).repeat(1, num_points, 1).view(
                [-1, init_pattern_encodings.shape[-1]])

            points_weights = self.point_segment_mlp(torch.cat([global_enc_propagated, point_features_flat], dim=-1))

        # ----- Getting per-panel features after attention application ------
        all_panel_features = []
        for panel_id in range(points_weights.shape[-1]):
            # get weights for particular panel
            panel_att_weights = points_weights[:, panel_id].unsqueeze(-1)

            # weight and pool to get panel encoding
            weighted_features = panel_att_weights * point_features_flat

            # same pool as in intial extractor
            panel_feature = self.feature_extractor.global_pool(weighted_features, batch, batch_size) 
            panel_feature = self.panel_dec_lin(panel_feature)  # reshape as needed
            panel_feature = panel_feature.view(batch_size, -1, panel_feature.shape[-1])

            all_panel_features.append(panel_feature)

        panel_encodings = torch.cat(all_panel_features, dim=1)  # concat in pattern dimention
        panel_encodings = panel_encodings.view(batch_size, -1, panel_encodings.shape[-1])

        points_weights = points_weights.view(batch_size, -1, points_weights.shape[-1]) if self.save_att_weights else []

        return panel_encodings, points_weights

    def forward(self, positions_batch, **kwargs):
        """3D to pattern with attention on per-point features"""

        batch_size = positions_batch.shape[0]

        # attention-based panel encodings
        panel_encodings, att_weights = self.forward_panel_enc_from_3d(positions_batch)

        # ---- decode panels from encodings ----
        panels = self.forward_panel_decode(panel_encodings.view(-1, panel_encodings.shape[-1]), batch_size)

        if len(att_weights) > 0:
            panels.update(att_weights=att_weights)  # save attention weights if non-empty

        return panels


# ----------- Stitches (independent) predictions ---------
class StitchOnEdge3DPairs(BaseModule):
    """
        Predicting status of a particular pair of edges (defined in 3D) -- whether they are connected 
        with a stitch or not.

        Binary classification problem
    """

    def __init__(self, data_config, config={}, in_loss_config={}):
        super().__init__()

        # data props
        self.pair_feature_len = data_config['element_size']

        # ---- Net configuration ----
        self.config.update({
            'stitch_hidden_size': 200, 
            'stitch_mlp_n_layers': 3
        })
        # update with input settings
        self.config.update(config) 

        # --- Losses ---
        self.config['loss'] = {
            'loss_components': ['edge_pair_class'],
            'quality_components': ['edge_pair_class', 'edge_pair_stitch_recall'],
            'panel_origin_invariant_loss': False,  # don't even try to evaluate
            'panel_order_inariant_loss': False
        }
        self.config['loss'].update(in_loss_config)  # apply input settings 

        # create loss!
        self.loss = ComposedLoss(data_config, self.config['loss'])
        self.config['loss'] = self.loss.config  # sync

        # ------ Modules ----
        mid_layers = [self.config['stitch_hidden_size']] * self.config['stitch_mlp_n_layers']
        self.mlp = blocks.MLP([self.pair_feature_len] + mid_layers + [1])


    def forward(self, pairs_batch, **kwargs):
        self.device = pairs_batch.device
        self.batch_size = pairs_batch.size(0)
        return_shape = list(pairs_batch.shape)
        return_shape.pop(-1)

        # reduce extra dimentions if needed
        out = self.mlp(pairs_batch.contiguous().view(-1, pairs_batch.shape[-1])) 

        # follow the same dimentions structure
        return out.view(return_shape)



if __name__ == "__main__":
    # Basic debug of the net classes

    torch.manual_seed(125)

    a = torch.arange(1, 25, dtype=torch.float)
    dataset_gt = a.view(-1, 2, 3)
    gt_batch = a.view(2, -1, 2, 3)  # ~ 2 examples in batch
    net = GarmentFullPattern3D(
        gt_batch.shape[3], gt_batch.shape[2], gt_batch.shape[1], 6, 3)  # {'shift': dataset_gt.mean(), 'scale': dataset_gt.std()})

    positions = torch.arange(1, 37, dtype=torch.float)
    features_batch = positions.view(2, -1, 3)  # note for the same batch size

    print('In batch shape: {}; Out batch shape: {}'.format(features_batch.shape, gt_batch.shape))
    print(net(features_batch)) 
    loss = net.loss(features_batch, gt_batch)
    print(loss)
    loss.backward()  # check it doesn't fail
