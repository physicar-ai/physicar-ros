"""pt -> onnx for the Racing model store.

    python3 convert.py model.pt model.onnx

The .pt checkpoint (a PhysicarNet state_dict) is the canonical, portable
form of a model; this derives the deployable ONNX from it. Loading with
strict=True doubles as the validity check — only a real PhysiCar
checkpoint converts. weights_only keeps a hostile pickle from running
code during the load.
"""
import os
import sys

import torch
import torch.nn as nn

CAMERA_W, CAMERA_H = 160, 120
N_ACTIONS = 3


class PhysicarNet(nn.Module):
    """Small CNN: camera image in -> action scores out (the ONE network
    every Racing course trains and drives)."""

    def __init__(self):
        super().__init__()
        self.cnn = nn.Sequential(
            nn.Conv2d(3, 32, 8, 4), nn.ReLU(),
            nn.Conv2d(32, 64, 4, 2), nn.ReLU(),
            nn.Conv2d(64, 64, 3, 1), nn.ReLU(), nn.Flatten())
        with torch.no_grad():
            n = self.cnn(torch.zeros(1, 3, CAMERA_H, CAMERA_W)).shape[1]
        self.head = nn.Sequential(
            nn.Linear(n, 256), nn.ReLU(),
            nn.Linear(256, N_ACTIONS))

    def forward(self, camera):
        x = camera / 255.0 * 2.0 - 1.0                    # 0-255 -> -1..1
        return torch.softmax(self.head(self.cnn(x)), dim=1)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    net = PhysicarNet()
    net.load_state_dict(torch.load(src, map_location="cpu", weights_only=True))
    net.eval()
    torch.onnx.export(
        net, (torch.zeros(1, 3, CAMERA_H, CAMERA_W),),
        dst + ".tmp", input_names=["camera"], output_names=["actions"],
        opset_version=17, dynamo=False)
    os.replace(dst + ".tmp", dst)
    print("converted ->", dst)


if __name__ == "__main__":
    main()
