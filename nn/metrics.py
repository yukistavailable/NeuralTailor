"""List of metrics to evalute on a model and a dataset"""

import torch
import torch.nn as nn
from data import Garment3DPatternFullDataset as PatternDataset


# ------- custom metrics --------
class PanelLoopLoss():
    """Evaluate loss for the panel edge sequence representation property: 
        ensuring edges within panel loop & return to origin"""
    def __init__(self, data_stats={}):
        """Info for evaluating padding vector if data statistical info is applied to it.
            * if standardization/normalization transform is applied to padding, 'data_stats' should be provided
                'data_stats' format: {'shift': <torch.tenzor>, 'scale': <torch.tensor>} 
        """
        self._eval_pad_vector(data_stats)
            
    def __call__(self, predicted_panels, original_panels=None, data_stats={}):
        """Evaluate loop loss on provided predicted_panels batch.
            * 'original_panels' are used to evaluate the correct number of edges of each panel in case padding is applied.
                If 'original_panels' is not given, it is assumed that there is no padding
                If data stats are not provided at init or in this call, zero vector padding is assumed
            * data_stats can be used to update padding vector on the fly
        """
        # flatten input into list of panels
        if len(predicted_panels.shape) > 3:
            predicted_panels = predicted_panels.view(-1, predicted_panels.shape[-2], predicted_panels.shape[-1])
        
        
        # prepare for padding comparison
        with_unpadding = original_panels is not None and original_panels.nelement() > 0  # existing non-empty tensor
        if with_unpadding:
            # flatten if not already 
            if len(original_panels.shape) > 3:
                original_panels = original_panels.view(-1, original_panels.shape[-2], original_panels.shape[-1])
            if data_stats:  # update pad vector
                self._eval_pad_vector(data_stats)
            if self.pad_tenzor is None:  # still not defined -> assume zero vector for padding
                self.pad_tenzor = torch.zeros(original_panels.shape[-1])
            pad_tenzor_propagated = self.pad_tenzor.repeat(original_panels.shape[1], 1)
            pad_tenzor_propagated = pad_tenzor_propagated.to(device=predicted_panels.device)
            
        # evaluate loss
        panel_coords_sum = torch.zeros((predicted_panels.shape[0], 2))
        panel_coords_sum = panel_coords_sum.to(device=predicted_panels.device)
        for el_id in range(predicted_panels.shape[0]):
            if with_unpadding:
                # panel original length
                panel = original_panels[el_id]
                # unpaded length
                bool_matrix = torch.isclose(panel, pad_tenzor_propagated, atol=1.e-2)
                seq_len = (~torch.all(bool_matrix, axis=1)).sum()  # only non-padded rows
            else:
                seq_len = len(predicted_panels[el_id])

            # get per-coordinate sum of edges endpoints of each panel
            panel_coords_sum[el_id] = predicted_panels[el_id][:seq_len, :2].sum(axis=0)

        panel_square_sums = panel_coords_sum ** 2  # per sum square

        # batch mean of squared norms of per-panel final points:
        return panel_square_sums.sum() / len(panel_square_sums)

    def _eval_pad_vector(self, data_stats={}):
        # prepare padding vector for unpadding the panel data on call
        if data_stats:
            shift = torch.Tensor(data_stats['shift'])
            scale = torch.Tensor(data_stats['scale'])
            self.pad_tenzor = - shift / scale
        else:
            self.pad_tenzor = None


class PatternStitchLoss():
    """Evalute the quality of stitching tags provided for every edge of a pattern:
        * Free edges have tags close to zero
        * Edges connected by a stitch have the same tag
        * Edges belonging to different stitches have 
    """
    def __init__(self, triplet_margin=0.1):
        self.triplet_margin = triplet_margin

    def __call__(self, stitch_tags, gt_stitches, gt_free_mask):
        """
        * stitch_tags contain tags for every panel in every pattern in the batch
        * gt_stitches contains the list of edge pairs that are stitches together.
            * with every edge indicated as (panel_id, edge_id) 
        """
        gt_stitches = gt_stitches.long()
        tag_len = stitch_tags.shape[-1]
        batch_size = stitch_tags.shape[0]
        num_stitches = gt_stitches.shape[-1]

        flat_stitch_tags = stitch_tags.view(batch_size, -1, stitch_tags.shape[-1])

        # https://stackoverflow.com/questions/55628014/indexing-a-3d-tensor-using-a-2d-tensor
        left_sides = flat_stitch_tags[torch.arange(batch_size).unsqueeze(-1), gt_stitches[:, 0, :]]
        right_sides = flat_stitch_tags[torch.arange(batch_size).unsqueeze(-1), gt_stitches[:, 1, :]]
        total_tags = torch.cat([left_sides, right_sides], dim=1)

        # tags on both sides of the stitch -- together
        similarity_loss = (left_sides - right_sides) ** 2
        similarity_loss = similarity_loss.sum() / (batch_size * num_stitches * 2 * tag_len)

        # Push tags to be non-zero
        non_zero_loss = self.triplet_margin - (total_tags ** 2).sum(dim=-1) / tag_len
        non_zero_loss = torch.max(non_zero_loss, torch.zeros_like(non_zero_loss)).sum() / (batch_size * num_stitches * 2)

        # Push tags away from each other
        total_neg_loss = []
        for pattern_tags in total_tags:  # per pattern in batch
            for tag_id, tag in enumerate(pattern_tags):
                # Evaluate distance to other tags
                neg_loss = (tag - pattern_tags) ** 2

                # compare with margin
                neg_loss = self.triplet_margin - neg_loss.sum(dim=-1) / tag_len  # single value per other tag

                # zero out losses for entries that should be equal to current tag
                neg_loss[tag_id] = 0  # torch.zeros_like(neg_loss[tag_id]).to(neg_loss.device)
                brother_id = tag_id + num_stitches if tag_id < num_stitches else tag_id - num_stitches
                neg_loss[brother_id] = 0  # torch.zeros_like(neg_loss[tag_id]).to(neg_loss.device)

                # ignore elements far enough from current tag
                neg_loss = torch.max(neg_loss, torch.zeros_like(neg_loss))

                # fin total
                total_neg_loss.append(neg_loss.sum() / len(neg_loss))
        # average neg loss per tag
        total_neg_loss = sum(total_neg_loss) / len(total_neg_loss)

        # free edges
        free_edges_loss = self.free_edges(stitch_tags, gt_free_mask)
               
        # final sum
        fin_stitch_losses = similarity_loss + non_zero_loss + total_neg_loss + free_edges_loss
        stitch_loss_dict = dict(
            stitch_similarity_loss=similarity_loss,
            stitch_non_zero_loss=non_zero_loss, 
            stitch_neg_loss=total_neg_loss, 
            free_edges_loss=free_edges_loss
        )

        return fin_stitch_losses, stitch_loss_dict

    def free_edges(self, stitch_tags, gt_free_mask):
        """Calculate loss for free edges (not part of any stitch)"""

        free_edges_slice = stitch_tags[gt_free_mask]
        # average norm per edge
        free_edge_loss = (free_edges_slice ** 2).sum() / free_edges_slice.shape[0]
        return free_edge_loss


class PatternStitchPrecisionRecall():
    """Evaluate Precision and Recall scores for pattern stitches prediction
        NOTE: It's NOT a diffentiable evaluation
    """

    def __init__(self):
        pass

    def __call__(self, stitch_tags, gt_stitches):
        """
         Evaluate on the batch of stitch tags
        """
        tot_precision = 0
        tot_recall = 0
        for pattern_idx in range(stitch_tags.shape[0]):
            stitch_list = PatternDataset.tags_to_stitches(stitch_tags[pattern_idx]).to(gt_stitches.device)
            num_detected_stitches = stitch_list.shape[1] if stitch_list.numel() > 0 else 0
            if not num_detected_stitches:  # no stitches detected -- zero recall & precision
                continue
            num_actual_stitches = gt_stitches[pattern_idx].shape[-1]
            
            # compare stitches
            correct_stitches = 0
            for detected in stitch_list.transpose(0, 1):
                for actual in gt_stitches[pattern_idx].transpose(0, 1):
                    # order-invariant comparison of stitch sides
                    correct_stitches += (all(detected == actual) or all(detected == actual.flip([0])))

            # precision -- how many of the detected stitches are actually there
            tot_precision += correct_stitches / num_detected_stitches if num_detected_stitches else 0
            # recall -- how many of the actual stitches were detected
            tot_recall += correct_stitches / num_actual_stitches if num_actual_stitches else 0
        
        # evrage by batch
        return tot_precision / stitch_tags.shape[0], tot_recall / stitch_tags.shape[0]

    def on_loader(self, data_loader, model):
        """Evaluate recall&precision of stitch detection on the full data loader"""

        with torch.no_grad():
            tot_precision = tot_recall = 0
            for batch in data_loader:
                predictions = model(batch['features'])
                batch_precision, batch_recall = self(predictions['stitch_tags'], batch['ground_truth']['stitches'])
                tot_precision += batch_precision
                tot_recall += batch_recall

        return tot_precision / len(data_loader), tot_recall / len(data_loader)


# ------- Model evaluation shortcut -------------
def eval_metrics(model, data_wrapper, section='test'):
    """Evalutes current model on the given dataset section"""
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model.to(device)
    model.eval()

    with torch.no_grad():
        current_metrics = {}
        loader = data_wrapper.get_loader(section)
        if loader:
            current_metrics = dict.fromkeys(['full_loss'], 0)
            model_defined = 0
            loop_loss = 0
            for batch in loader:
                features, gt = batch['features'].to(device), batch['ground_truth']
                if gt is None or (hasattr(gt, 'nelement') and gt.nelement() == 0):  # assume reconstruction task
                    gt = features

                # loss evaluation
                full_loss, loss_dict = model.loss(features, gt)

                # summing up
                current_metrics['full_loss'] += full_loss
                for key, value in loss_dict.items():
                    if key not in current_metrics:
                        current_metrics[key] = 0  # init new metric
                    current_metrics[key] += value

            # normalize & convert
            for metric in current_metrics:
                if isinstance(current_metrics[metric], torch.Tensor):
                    current_metrics[metric] = current_metrics[metric].cpu().numpy()  # conversion only works on cpu
                current_metrics[metric] /= len(loader)
    
    return current_metrics


if __name__ == "__main__":
    # debug

    stitch_eval = PatternStitchPrecisionRecall()

    tags = torch.FloatTensor(
        [[
            [
                [0, 0, 0],
                [1.2, 3., 0],
                [0, 0, 0]
            ],
            [
                [0, 3., 0],
                [0, 0, 0],
                [1.2, 3., 0],
            ]
        ]]
    )
    stitches = torch.IntTensor([
        [
            [1, 5]
        ]
    ]).transpose(0, 1)

    print(stitch_eval(tags, stitches))
