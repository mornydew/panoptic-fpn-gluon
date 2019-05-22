"""Cityscapes Dataloader"""
import os
import json
import random
import numpy as np
from PIL import Image, ImageFilter

import cv2
import mxnet as mx
from .segbase import SegmentationDataset
from .base import VisionDataset
from .transforms import bbox as tbbox

"""
Cityscapes Panoptic Save Type:
channel 0: all zero, computed by pix // 256 // 256
channel 1: nonzero represent isinstance(thing) area, computed by pix // 256
channel 2: represent category label for each pixel, computed by pix % 256
"""
class CitysPanoptic(VisionDataset):
    """Cityscapes Dataloader"""
    # pylint: disable=abstract-method
    BASE_DIR = 'cityscapes'
    SEG_CLASS = ['road', 'sidewalk', 'building', 'wall', 'fence', 'pole', 'traffic light',
                  'traffic sign', 'vegetation', 'terrain', 'sky']
    MASK_CLASS = ['person', 'rider', 'car', 'truck', 'bus', 'train', 'motorcycle', 'bicycle']
    def __init__(self, root=os.path.expanduser('~/.mxnet/datasets/citys'), split='train',
                 base_size=1024, crop_size=(512, 1024), mode=None, transform=None, **kwargs):
        super(CitysPanoptic, self).__init__(root, **kwargs)
        self.split = split
        self.root = root
        self.mode = mode
        self.base_size = base_size
        self._transform = transform
        self.crop_size = crop_size
        self.images, self.mask_paths = _get_city_pairs(self.root, self.split)
        assert (len(self.images) == len(self.mask_paths))
        if len(self.images) == 0:
            raise RuntimeError("Found 0 images in subfolders of: \
                " + self.root + "\n")
        self.inst_mapping = {24: 0, 25: 1, 26: 2, 27: 3, 28: 4, 31: 5, 32: 6, 33: 7}
        self.inst_mapping_inv = {val:key for key, val in self.inst_mapping.items()}
        self.valid_classes = [7, 8, 11, 12, 13, 17, 19, 20, 21, 22,
                              23, 24, 25, 26, 27, 28, 31, 32, 33]
        self._key = np.array([-1, -1, -1, -1, -1, -1,
                              -1, -1, 0, 1, -1, -1,
                              2, 3, 4, -1, -1, -1,
                              5, -1, 6, 7, 8, 9,
                              10, 11, 12, 13, 14, 15,
                              -1, -1, 16, 17, 18])
        self._mapping = np.array(range(-1, len(self._key)-1)).astype('int32')
        self.inst_boxes = _get_city_boxes(self.root, self.inst_mapping, self.split)

    def _class_to_index(self, mask):
        # assert the values
        values = np.unique(mask)
        for value in values:
            assert(value in self._mapping)
        index = np.digitize(mask.ravel(), self._mapping, right=True)
        return self._key[index].reshape(mask.shape)

    def _sync_transform(self, img, segm, inst, bbox):
        # random mirror
        if random.random() < 0.5:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
            segm = segm.transpose(Image.FLIP_LEFT_RIGHT)
            inst = inst.transpose(Image.FLIP_LEFT_RIGHT)
            bbox = tbbox.flip(bbox, (img.width, img.height), flip_x=True)

        # type transform
        segm = np.array(segm)
        inst = np.array(inst)
        inst_mask = inst != 0
        inst = segm * inst_mask
        for cls_id, inst_id in bbox[:, 4:]:
            segm[segm == inst_id] = self.inst_mapping_inv[cls_id]

        # random crop
        ch, cw = self.crop_size
        short_size = random.randint(int(self.base_size*0.5), int(self.base_size*2.0))
        w, h = img.size
        if h > w:
            ow = short_size
            oh = int(1.0 * h * ow / w)
        else:
            oh = short_size
            ow = int(1.0 * w * oh / h)
        img = img.resize((ow, oh), Image.BILINEAR)
        segm = cv2.resize(segm, (ow, oh), interpolation=cv2.INTER_NEAREST)
        inst = cv2.resize(inst, (ow, oh), interpolation=cv2.INTER_NEAREST)
        bbox[:, :-2] *= (1.0 * oh / h)
        # random select a gt ad crop
        boxid = random.randrange(len(bbox))
        selected_box = bbox[boxid]
        x1, y1, x2, y2, _, _ = selected_box
        ctr_x = (x1 + x2) / 2
        ctr_y = (y1 + y2) / 2
        crop_x1 = max(0, ctr_x - cw/2)
        crop_y1 = max(0, ctr_y - ch/2)
        w, h = img.size
        img = img.crop((crop_x1, crop_y1, crop_x1+cw, crop_y1+ch))
        segm = segm[int(crop_y1):int(crop_y1)+ch, int(crop_x1):int(crop_x1)+cw]
        inst = inst[int(crop_y1):int(crop_y1)+ch, int(crop_x1):int(crop_x1)+cw]
        # crop bbox
        crop_box = (crop_x1, crop_y1, cw, ch)
        bbox = tbbox.crop(bbox, crop_box, allow_outside_center=False)

        # gaussian blur as in PSP
        if random.random() < 0.5:
            img = img.filter(ImageFilter.GaussianBlur(
                radius=random.random()))

        # transform inst to binary map
        binst = []
        for inst_id in bbox[:,-1]:
            binst.append(mx.nd.array(inst == inst_id).expand_dims(0))
        inst = mx.nd.concat(*binst, dim=0)

        # final transform
        img = self._img_transform(img)
        segm = self._mask_transform(segm)
        bbox = mx.nd.array(bbox[:,:-1]).astype('float32')
        return img, segm, inst, bbox

    def __getitem__(self, index):
        img = Image.open(self.images[index]).convert('RGB')
        mask = Image.open(self.mask_paths[index])
        segm, inst, _ = mask.split()
        imgid = self.images[index].split('/')[-1].split('.')[0]
        imgid = imgid.replace('_leftImg8bit', '')
        bbox = self.inst_boxes[imgid]
        # synchrosized transform
        if self.mode == 'train':
            img, segm, inst, bbox = self._sync_transform(img, segm, inst, bbox)
        elif self.mode == 'val':
            img, segm, inst, bbox = self._sync_transform(img, segm, inst, bbox)
        else:
            pass
        # general resize, normalize and toTensor
        if self._transform is not None:
            return self._transform(img, segm, inst, bbox)
        return img, segm, inst, bbox

    def _img_transform(self, img):
        return mx.nd.array(np.array(img), ctx=mx.cpu(0))

    def _mask_transform(self, mask):
        target = self._class_to_index(np.array(mask).astype('int32'))
        return mx.nd.array(target).astype('int32')

    def __len__(self):
        return len(self.images)

    @property
    def pred_offset(self):
        return 0


def _get_city_boxes(folder, mappings, split='train'):
    inst_path = os.path.join(folder, 'gtFine/cityscapes_panoptic_' + split + '.json')
    records = json.load(open(inst_path, 'r'))
    annots = records['annotations']
    result_boxes = {}
    for annot in annots:
        boxes = []
        segm_info = annot['segments_info']
        for segm in segm_info:
            x1, y1, w, h = segm['bbox']
            x2 = x1 + w - 1
            y2 = y1 + h - 1
            cls_id = segm['category_id']
            inst_id = segm['id']
            # remove the group instances
            if cls_id in mappings and cls_id != inst_id:
                cate = mappings[cls_id]
                inst_id = inst_id % 256
                boxes.append([x1, y1, x2, y2, cate, inst_id])
        segm_id = annot['image_id']
        result_boxes[segm_id] = np.array(boxes, dtype=np.float32)
    return result_boxes


def _get_city_pairs(folder, split='train'):
    def get_path_pairs(img_folder, mask_folder):
        img_paths = []
        mask_paths = []
        for root, _, files in os.walk(img_folder):
            for filename in files:
                if filename.endswith(".png"):
                    imgpath = os.path.join(root, filename)
                    foldername = os.path.basename(os.path.dirname(imgpath))
                    maskname = filename.replace('leftImg8bit', 'gtFine_panoptic')
                    maskpath = os.path.join(mask_folder, maskname)
                    if os.path.isfile(imgpath) and os.path.isfile(maskpath):
                        img_paths.append(imgpath)
                        mask_paths.append(maskpath)
                    else:
                        print('cannot find the mask or image:', imgpath, maskpath)
        print('Found {} images in the folder {}'.format(len(img_paths), img_folder))
        return img_paths, mask_paths

    if split in ('train', 'val'):
        img_folder = os.path.join(folder, 'leftImg8bit/' + split)
        mask_folder = os.path.join(folder, 'gtFine/cityscapes_panoptic_'+ split)
        img_paths, mask_paths = get_path_pairs(img_folder, mask_folder)
        return img_paths, mask_paths
    else:
        assert split == 'trainval'
        print('trainval set')
        train_img_folder = os.path.join(folder, 'leftImg8bit/train')
        train_mask_folder = os.path.join(folder, 'gtFine/cityscapes_panoptic_train')
        val_img_folder = os.path.join(folder, 'leftImg8bit/val')
        val_mask_folder = os.path.join(folder, 'gtFine/cityscapes_panoptic_val')
        train_img_paths, train_mask_paths = get_path_pairs(train_img_folder, train_mask_folder)
        val_img_paths, val_mask_paths = get_path_pairs(val_img_folder, val_mask_folder)
        img_paths = train_img_paths + val_img_paths
        mask_paths = train_mask_paths + val_mask_paths
    return img_paths, mask_paths
