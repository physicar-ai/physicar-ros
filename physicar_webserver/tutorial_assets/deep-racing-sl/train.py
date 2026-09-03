import json
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, Subset

# The action table: class labels the model learns AND the data/<key>/ folders.
ACTIONS = {
    "left": {"speed": 0.5, "steering": 20.0},
    "straight": {"speed": 0.5, "steering": 0.0},
    "right": {"speed": 0.5, "steering": -20.0},
}
CAMERA_W, CAMERA_H = 160, 120   # model input resolution

EPOCHS = 10
BATCH_SIZE = 64
LEARNING_RATE = 0.001
VAL_SPLIT = 0.2
MIN_PHOTOS = 100    # below this the model just memorizes — refuse to train

# The tutorial page's settings (gear) are saved per machine — apply overrides.
try:
    with open("settings.json") as _f:
        _cfg = json.load(_f)
    for _a in ACTIONS.values():
        _a["speed"] = float(_cfg.get("speed", _a["speed"]))
    ACTIONS["left"]["steering"] = abs(float(_cfg.get("left", 20.0)))
    ACTIONS["right"]["steering"] = -abs(float(_cfg.get("right", 20.0)))
except (OSError, ValueError):
    pass


class PhysicarNet(nn.Module):
    """Small CNN: camera image in -> action scores out.
    Normalization lives inside the network, so a raw image goes in."""

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
            nn.Linear(256, len(ACTIONS)))

    def forward(self, camera):
        x = camera / 255.0 * 2.0 - 1.0                    # 0-255 -> -1..1
        return torch.softmax(self.head(self.cnn(x)), dim=1)


class DrivingData(Dataset):
    def __init__(self, augment=False):
        self.augment = augment
        self.samples = []                       # (photo path, class index)
        for i, key in enumerate(ACTIONS):
            for f in sorted(Path("data", key).glob("*.jpg")):
                self.samples.append((f, i))
        if not self.samples:
            raise SystemExit("data/ is empty — collect photos on the Labeling page first")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, i):
        path, label = self.samples[i]
        img = cv2.imread(str(path))
        img = cv2.resize(img, (CAMERA_W, CAMERA_H), interpolation=cv2.INTER_AREA)
        if self.augment:    # random brightness/contrast, label stays the same
            img = cv2.convertScaleAbs(img, alpha=np.random.uniform(0.8, 1.2),
                                      beta=np.random.uniform(-30, 30))
        camera = torch.from_numpy(img.transpose(2, 0, 1).astype(np.float32))
        return camera, label


def main():
    data = DrivingData(augment=True)      # training: random lighting
    if len(data) < MIN_PHOTOS:
        raise SystemExit(f"only {len(data)} photos — too few to train well; "
                         f"collect at least {MIN_PHOTOS} on the Labeling page")
    plain = DrivingData()                 # validation: photos as-is
    n_val = max(1, int(len(data) * VAL_SPLIT))
    idx = torch.randperm(len(data)).tolist()
    train_set = Subset(data, idx[n_val:])
    val_set = Subset(plain, idx[:n_val])
    train_dl = DataLoader(train_set, BATCH_SIZE, shuffle=True)
    val_dl = DataLoader(val_set, BATCH_SIZE)
    print(f"training on {len(train_set)} photos, validating on {len(val_set)}"
          f" / {len(ACTIONS)} actions")

    # training curve on disk, rewritten every epoch (the Train page charts it)
    progress = {"photos": len(train_set), "epochs": EPOCHS, "history": []}
    Path("train_progress.json").write_text(json.dumps(progress))

    net = PhysicarNet()
    opt = torch.optim.Adam(net.parameters(), lr=LEARNING_RATE)

    for epoch in range(EPOCHS):
        net.train()
        for camera, y in train_dl:
            loss = F.nll_loss(net(camera).clamp_min(1e-8).log(), y)
            opt.zero_grad(); loss.backward(); opt.step()

        net.eval()
        correct = total = 0
        with torch.no_grad():
            for camera, y in val_dl:
                correct += (net(camera).argmax(1) == y).sum().item()
                total += len(y)
        print(f"epoch {epoch + 1:2d}/{EPOCHS}  accuracy {correct / total:.3f}")
        progress["history"].append({"epoch": epoch + 1, "accuracy": correct / total})
        Path("train_progress.json").write_text(json.dumps(progress))

    net.eval()
    # export to a temp name, then swap in atomically — a crash mid-export
    # must never destroy the previous good model
    torch.onnx.export(
        net, (torch.zeros(1, 3, CAMERA_H, CAMERA_W),),
        "model.onnx.tmp", input_names=["camera"], output_names=["actions"],
        opset_version=17, dynamo=False)
    Path("model.onnx.tmp").replace("model.onnx")
    print("\nsaved -> model.onnx")


if __name__ == "__main__":
    main()
