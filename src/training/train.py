#!/usr/bin/env python
# coding: utf-8

import argparse
import copy
import json
import os
import os.path as osp
import random
import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from dataset import PointCloud
from loss_functions import loss_DGNI, loss_sitzmann
from model import SIREN
from util import create_output_paths, load_experiment_parameters


def train_model(dataset, model, device, config) -> torch.nn.Module:
    epochs = config["epochs"]
    warmup_epochs = config.get("warmup_epochs", 0)

    epochs_til_checkpoint = config.get("epochs_to_checkpoint", 0)

    log_path = config["log_path"]
    loss_fn = config["loss_fn"]
    optim = config["optimizer"]

    train_loader = DataLoader(
        dataset,
        pin_memory=True,
        num_workers=0,
        drop_last=False,
    )
    model.to(device)

    # Creating the summary storage folder
    summary_path = osp.join(log_path, 'summaries')
    if not osp.exists(summary_path):
        os.makedirs(summary_path)
    writer = SummaryWriter(summary_path)

    losses = dict()
    best_loss = np.inf
    best_weights = None
    for epoch in range(epochs):
        running_loss = dict()
        for i, (input_data, gt_data) in enumerate(train_loader, start=0):

            # get the inputs; data is a list of [inputs, labels]
            inputs = {k: v.to(device) for k, v in input_data.items()}
            gt = {k: v.to(device) for k, v in gt_data.items()}
            
            # zero the parameter gradients
            optim.zero_grad()
            
            # forward + backward + optimize
            outputs = model( torch.cat( [inputs["distance"], inputs["coords"]], axis=2 ) )
            
            loss = loss_fn(outputs, gt)

            train_loss = torch.zeros((1, 1), device=device)
            for it, l in loss.items():
                train_loss += l
                # accumulating statistics per loss term
                if it not in running_loss:
                    running_loss[it] = l.item()
                else:
                    running_loss[it] += l.item()

            train_loss.backward()
            optim.step()

            writer.add_scalar("train_loss", train_loss.item(), epoch)

            break

        # accumulate statistics
        for it, l in running_loss.items():
            if it in losses:
                losses[it][epoch] = l
            else:
                losses[it] = [0.] * epochs
                losses[it][epoch] = l
            writer.add_scalar(it, l, epoch)

        epoch_loss = 0
        for k, v in running_loss.items():
            epoch_loss += v
        print(f"Epoch: {epoch} - Loss: {epoch_loss}")

        # Saving the best model after warmup.
        if epoch > warmup_epochs and epoch_loss < best_loss:
            best_loss = epoch_loss
            best_weights = copy.deepcopy(model.state_dict())
            torch.save(
                best_weights,
                osp.join(log_path, "models", "model_best.pth")
            )

        # saving the model at checkpoints
        if epoch and epochs_til_checkpoint and not \
           epoch % epochs_til_checkpoint:
            print(f"Saving model for epoch {epoch}")
            torch.save(
                model.state_dict(),
                osp.join(log_path, "models", f"model_{epoch}.pth")
            )
        else:
            torch.save(
                model.state_dict(),
                osp.join(log_path, "models", "model_current.pth")
            )



    return losses, best_weights


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        usage="python main.py path_to_experiments.json"
    )

    p.add_argument(
        "experiment_path", type=str,
        help="Path to the JSON experiment description file"
    )
    args = p.parse_args()
    parameter_dict = load_experiment_parameters(args.experiment_path)

    if not bool(parameter_dict):
        raise ValueError("JSON experiment not found")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    seed = 123
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    full_path = create_output_paths(
        parameter_dict["checkpoint_path"],
        parameter_dict["experiment_name"],
        overwrite=False
    )

    # Saving the parameters to the output path
    with open(osp.join(full_path, "params.json"), "w+") as fout:
        json.dump(parameter_dict, fout, indent=4)

    sampling_config = parameter_dict["sampling_opts"]
    dataset = PointCloud(
        jsonPath= parameter_dict["dataset"],
        batchSize= parameter_dict["batch_size"],
        curvatureFractions=sampling_config["curvature_iteration_fractions"],
        curvaturePercentiles=sampling_config["curvature_percentile_thresholds"]
    )

    network_params = parameter_dict["network"]
    model = SIREN(
        n_in_features=4,
        n_out_features=1,
        hidden_layer_config=network_params["hidden_layer_nodes"],
        w0=network_params["w0"],
        ww=network_params.get("ww", None)
    )
    print(model)

    opt_params = parameter_dict["optimizer"]
    if opt_params["type"] == "adam":
        optimizer = torch.optim.Adam(
            lr=opt_params["lr"],
            params=model.parameters()
        )

    config_dict = {
        "epochs": parameter_dict["num_epochs"],
        "warmup_epochs": parameter_dict.get("warmup_epochs", 0),
        "batch_size": parameter_dict["batch_size"],
        "epochs_to_checkpoint": parameter_dict["epochs_to_checkpoint"],
        "log_path": full_path,
        "optimizer": optimizer,
        "loss_fn": loss_DGNI if parameter_dict == 'dgni' else loss_sitzmann
    }

    losses, best_weights = train_model(
        dataset,
        model,
        device,
        config_dict,
    )
    loss_df = pd.DataFrame.from_dict(losses)
    loss_df.to_csv(osp.join(full_path, "losses.csv"), sep=";", index=None)

    # saving the final model.
    torch.save(
        model.state_dict(),
        osp.join(full_path, "models", "model_final.pth")
    )
