# gap.py
# (c) 2013 Mikael Vejdemo-Johansson for the code structure
# (c) 2021 Maria Korosteleva for PyTorch adaptation and the rest of the changes
# BSD License
#
# PyTorch-based function to compute the gap statistic and standard error for evaluating k-means clustering.
# Gap statistic defined in
# Tibshirani, Walther, Hastie:
#  Estimating the number of clusters in a data set via the gap statistic
#  J. R. Statist. Soc. B (2001) 63, Part 2, pp 411-423  (http://web.stanford.edu/~hastie/Papers/gap.pdf)


import torch 
from kmeans_pytorch import kmeans  # https://github.com/subhadarship/kmeans_pytorch


def gap_torch(data, refs=None, nrefs=20, ks=range(1, 11)):
    """
        Compute the Gap statistic for an n x m dataset in data (presented as torch.Tensor).

        Either give a precomputed set of reference distributions in *refs* as an (nrefs, n, m) torch tensor,
        or state the number *nrefs* of reference distributions for automatic generation with a
        uniformed distribution within the bounding box of data.

        Give the list of k-values for which you want to compute the statistic in ks.

        Devices note: all computations are performed on the device where the *data* is located. 
        Both CPU and GPU are supported.
    """
    shape = data.shape
    if refs is None:
        tops, _ = data.max(dim=0)
        bots, _ = data.min(dim=0)
        dists = tops - bots 

        if torch.allclose(tops, bots, atol=0.1):  # degenerate case, no need for further processing
            labels, cluster_center = _single_cluster_kmeans(data)
            return [None] * len(ks), [None] * len(ks), labels, [cluster_center]

        # uniform distribution
        rands = torch.rand((nrefs, shape[0], shape[1]), device=data.device)
        rands = rands * dists + bots
    else:
        rands = refs
        nrefs = refs.shape[0]

    gaps = [None] * len(ks)  # lists allow for None values
    std_errors = [None] * len(ks)
    labels_per_k = []
    ccs_per_k = []
    for (i, k) in enumerate(ks):   
        # Step 1 -- clustering
        # on the data
        if k == 1:
            labels, cluster_centers = _single_cluster_kmeans(data)
        else:
            labels, cluster_centers = kmeans(X=data, num_clusters=k, device=data.device, tqdm_flag=False)
            labels, cluster_centers = labels.to(data.device), cluster_centers.to(data.device)
        labels_per_k.append(labels)  # save labels to return
        ccs_per_k.append(cluster_centers)

        disp = sum([torch.dist(data[m], cluster_centers[labels[m]]) for m in range(shape[0])])

        # on the reference distributions
        refdisps = torch.zeros(nrefs, device=data.device)
        for j in range(nrefs):
            if k == 1:
                labels, cluster_centers = _single_cluster_kmeans(rands[j])
            else:
                labels, cluster_centers = kmeans(X=rands[j], num_clusters=k, device=rands.device, tqdm_flag=False)
                labels, cluster_centers = labels.to(rands.device), cluster_centers.to(rands.device)

            refdisps[j] = sum([torch.dist(rands[j, m], cluster_centers[labels[m]]) for m in range(shape[0])])

        # Step 2 -- gaps
        # flipped mean & log https://gist.github.com/michiexile/5635273#gistcomment-2324237
        reflogs = torch.log(refdisps)
        refmean = torch.mean(reflogs)
        gaps[i] = refmean - torch.log(disp)
        
        # Step 3 -- standard errors
        std_errors[i] = torch.sqrt(torch.mean((reflogs - refmean) ** 2) * (1 + 1. / nrefs))

    return gaps, std_errors, labels_per_k[-1] if len(labels_per_k) else None, ccs_per_k


def _single_cluster_kmeans(data):
    """
        Evaluate cluster center and give the set of labels in KMeans format 
        when the requested K=1
    """
    cluster_center = data.mean(dim=0).unsqueeze(0)
    labels = torch.zeros(data.shape[0], dtype=torch.int, device=data.device)

    return labels, cluster_center
