import json
import os
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

# The action table: class labels the model learns AND ml/labeling_data/<key>/.
ACTIONS = {
    "left": {"speed": 0.5, "steering": 20.0},
    "straight": {"speed": 0.5, "steering": 0.0},
    "right": {"speed": 0.5, "steering": -20.0},
}
CAMERA_W, CAMERA_H = 160, 120   # model input resolution

EPOCHS = 10
BATCH_SIZE = 64
LEARNING_RATE = 0.001
MIN_PHOTOS = 100    # below this the model just memorizes — refuse to train

# Continue training from this model — rides on the run request (empty = new)
BASE_MODEL = os.environ.get("RACING_BASE", "")

# The Racing panel's settings (gear) are saved per machine and SHARED by the
# supervised and reinforcement courses — apply the overrides.
try:
    with open("ml/settings.json") as _f:
        _cfg = json.load(_f)
    for _a in ACTIONS.values():
        _a["speed"] = float(_cfg.get("speed", _a["speed"]))
    ACTIONS["left"]["steering"] = abs(float(_cfg.get("left", 20.0)))
    ACTIONS["right"]["steering"] = -abs(float(_cfg.get("right", 20.0)))
except (OSError, ValueError):
    pass
if BASE_MODEL and not os.path.exists(f"ml/models/{BASE_MODEL}.pt"):
    print(f"base model {BASE_MODEL} has no checkpoint — starting from scratch")
    BASE_MODEL = ""


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
            for f in sorted(Path("ml/labeling_data", key).glob("*.jpg")):
                self.samples.append((f, i))
        if not self.samples:
            raise SystemExit("ml/labeling_data/ is empty — collect photos on the Labeling section first")

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
    data = DrivingData(augment=True)      # random lighting so it generalizes
    if len(data) < MIN_PHOTOS:
        raise SystemExit(f"only {len(data)} photos — too few to train well; "
                         f"collect at least {MIN_PHOTOS} on the Labeling page")
    train_dl = DataLoader(data, BATCH_SIZE, shuffle=True)
    print(f"training on {len(data)} photos / {len(ACTIONS)} actions")

    # training curve on disk, rewritten every epoch (the Train step charts it)
    progress = {"photos": len(data), "epochs": EPOCHS, "history": []}
    Path("ml/train_progress.json").write_text(json.dumps(progress))

    net = PhysicarNet()
    if BASE_MODEL:
        net.load_state_dict(torch.load(f"ml/models/{BASE_MODEL}.pt",
                                       map_location="cpu", weights_only=True))
        print(f"continuing from model {BASE_MODEL}")
    opt = torch.optim.Adam(net.parameters(), lr=LEARNING_RATE)

    try:
        for epoch in range(EPOCHS):
            net.train()
            correct = total = 0
            for camera, y in train_dl:
                out = net(camera)
                loss = F.nll_loss(out.clamp_min(1e-8).log(), y)
                opt.zero_grad(); loss.backward(); opt.step()
                # accuracy: how many of this batch the net already gets right
                correct += (out.argmax(1) == y).sum().item()
                total += len(y)
            print(f"epoch {epoch + 1:2d}/{EPOCHS}  accuracy {correct / total:.3f}")
            progress["history"].append({"epoch": epoch + 1, "accuracy": correct / total})
            Path("ml/train_progress.json").write_text(json.dumps(progress))
            # checkpoint after EVERY epoch — a Stop or crash never loses more
            # than the epoch in flight; when this process ends (any way at
            # all) the runner files the last checkpoint into the model store
            torch.save(net.state_dict(), "ml/checkpoint.pt.tmp")
            Path("ml/checkpoint.pt.tmp").replace("ml/checkpoint.pt")
    except KeyboardInterrupt:
        print("\nstopped — the last finished epoch's checkpoint is kept")


if __name__ == "__main__":
    main()
