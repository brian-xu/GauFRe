import json
import os
import random
from argparse import ArgumentParser
from math import ceil, floor

import cv2
import numpy as np
import open_clip
import torch
import torch.nn as nn
from einops import rearrange
from PIL import Image
from torchvision.transforms import v2
from torchvision.transforms.functional import InterpolationMode
from tqdm import tqdm

DINO_FEATURES = 768
CLIP_FEATURES = 512

dino = torch.hub.load("facebookresearch/dinov2", "dinov2_vitb14")
dino.cuda()
dino.eval()

clip, _, preprocess = open_clip.create_model_and_transforms(
    "ViT-B-32", pretrained="laion2b_s34b_b79k"
)
clip.cuda()
clip.eval()


def extract_features(image_list, resolution, sequence_dir):
    images_dir = os.path.join(sequence_dir, f"rgb/{resolution}")
    mask_dir = os.path.join(sequence_dir, f"object_mask/{resolution}")
    dino_dir = os.path.join(sequence_dir, f"dino/{resolution}")
    clip_dir = os.path.join(sequence_dir, f"clip/{resolution}")
    os.makedirs(dino_dir, exist_ok=True)
    os.makedirs(clip_dir, exist_ok=True)
    _, dirs, _ = [p for p in os.walk(mask_dir)][0]
    most_visible = {}
    progress_bar = tqdm(range(0, len(files)), desc="Extracting DINO features")

    for im_tensor, file in image_list:
        H, W, _ = im_tensor.shape
        im_tensor = im_tensor.unsqueeze(0).cuda()
        im_tensor = rearrange(im_tensor, "b h w c -> b c h w")
        H_14, W_14 = ceil(H / 14) * 14, ceil(W / 14) * 14
        padding_top, padding_bottom = ceil((H_14 - H) / 2), floor((H_14 - H) / 2)
        padding_left, padding_right = ceil((W_14 - W) / 2), floor((W_14 - W) / 2)
        assert (
            H + padding_top + padding_bottom == H_14
            and W + padding_left + padding_right == W_14
        )
        patch_padding = nn.ReflectionPad2d(
            (padding_left, padding_right, padding_top, padding_bottom)
        )

        with torch.no_grad():
            dino_dict = dino.forward_features(patch_padding(im_tensor))
        dino_features = dino_dict["x_norm_patchtokens"]
        dino_features = rearrange(dino_features, "b s c -> b c s")
        dino_features = torch.reshape(
            dino_features, (1, DINO_FEATURES, H_14 // 14, W_14 // 14)
        )
        dino_features = rearrange(dino_features, "b c h w -> b h w c")
        dino_crop = {
            "padding_top": padding_top,
            "padding_bottom": padding_bottom,
            "padding_left": padding_left,
            "padding_right": padding_right,
        }
        np.save(os.path.join(dino_dir, file), dino_features.squeeze(0).cpu().numpy())
        with open(os.path.join(dino_dir, file + ".json"), "w") as f:
            json.dump(dino_crop, f)
        for dir in dirs:
            mask_path = os.path.join(mask_dir, dir, file + ".png")
            mask = Image.open(mask_path)
            mask.resize((W, H))

            mask_data = np.array(mask.convert("RGB"))
            mask_data //= max(1, mask_data.max())
            if dir not in most_visible:
                most_visible[dir] = [mask_data.sum(), file]
            elif mask_data.sum() > most_visible[dir][0]:
                most_visible[dir] = [mask_data.sum(), file]
        progress_bar.update(1)
    progress_bar.close()

    # Create a mapping from directory names to indices
    dir_to_idx = {dir_name: idx for idx, dir_name in enumerate(sorted(dirs))}
    
    clip_embeddings = np.zeros((len(most_visible), CLIP_FEATURES))
    progress_bar = tqdm(range(0, len(most_visible)), desc="Extracting CLIP features")
    for dir in most_visible:
        _, file = most_visible[dir]
        image_path = os.path.join(images_dir, file)
        image = Image.open(image_path)

        im_data = np.array(image.convert("RGB"))
        H, W, _ = im_data.shape
        mask_path = os.path.join(mask_dir, dir, file + ".png")
        mask = Image.open(mask_path)

        mask_data = np.array(mask.convert("RGB"))
        mask_data //= max(1, mask_data.max())
        masked_image = Image.fromarray(mask_data * image)
        preprocessed = preprocess(masked_image).unsqueeze(0).cuda()
        with torch.no_grad():
            clip_embedding = clip.encode_image(preprocessed)
        clip_embeddings[dir_to_idx[dir], :] = clip_embedding.squeeze().cpu().numpy()
        progress_bar.update(1)
    progress_bar.close()
    np.save(os.path.join(clip_dir, "embeddings"), clip_embeddings)


def seed_everything(seed_value):
    random.seed(seed_value)
    np.random.seed(seed_value)
    torch.manual_seed(seed_value)
    os.environ["PYTHONHASHSEED"] = str(seed_value)

    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed_value)
        torch.cuda.manual_seed_all(seed_value)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = True


if __name__ == "__main__":
    seed_num = 42
    seed_everything(seed_num)

    parser = ArgumentParser(description="Feature Extraction parameters")
    parser.add_argument("-s", "--sequence", type=str, required=True)
    parser.add_argument("-r", "--resolution", type=int, default=2)
    parser.add_argument("-d", "--downsample", type=int, default=-1)
    args = parser.parse_args()

    images_dir = os.path.join(args.sequence, f"rgb/{args.resolution}x")
    _, _, files = [p for p in os.walk(images_dir)][0]
    img_list = []

    for file in files:
        image = cv2.imread(os.path.join(images_dir, file))

        orig_w, orig_h = image.shape[1], image.shape[0]
        if args.downsample == -1:
            if orig_h > 1080:
                if not WARNED:
                    print(
                        "[ INFO ] Encountered quite large input images (>1080P), rescaling to 1080P.\n "
                        "If this is not desired, please explicitly specify '--resolution/-r' as 1"
                    )
                    WARNED = True
                global_down = orig_h / 1080
            else:
                global_down = 1
        else:
            global_down = orig_w / args.downsample

        scale = float(global_down)
        resolution = (int(orig_w / scale), int(orig_h / scale))

        image = cv2.resize(image, resolution)
        image = torch.tensor(image, dtype=torch.float)
        img_list.append([image / 255, file])

    extract_features(img_list, str(args.resolution) + "x", args.sequence)
