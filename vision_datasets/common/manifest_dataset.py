import logging
import os.path
import pathlib
from abc import ABC, abstractmethod
from copy import deepcopy
import random
from PIL import Image
from tqdm import tqdm

from .base_dataset import BaseDataset
from .constants import DatasetTypes
from .dataset_info import BaseDatasetInfo
from .data_manifest import DatasetManifest, ImageDataManifest
from .image_loader import PILImageLoader
from .util import FileReader

logger = logging.getLogger(__name__)


class ManifestDataset(BaseDataset):
    """Dataset class that accesses data from dataset manifest
    """

    def __init__(self, dataset_info: BaseDatasetInfo, dataset_manifest: DatasetManifest, coordinates='relative', dataset_resources=None):
        """

        Args:
            dataset_info (BaseDatasetInfo): dataset info, containing high level information about the dataset, such as name, type, description, etc
            dataset_manifest (DatasetManifest): dataset manifest containing meta data such as image paths, annotations, etc
            coordinates (str): 'relative' or 'absolute', indicating the desired format of the bboxes returned.
            dataset_resources (str): disposable resources associated with this dataset
        """
        assert dataset_manifest is not None
        assert coordinates in ['relative', 'absolute']

        super().__init__(dataset_info)

        self.dataset_manifest = dataset_manifest
        self.coordinates = coordinates
        self._file_reader = FileReader()
        self.dataset_resources = dataset_resources

    @property
    def labels(self):
        return self.dataset_manifest.labelmap

    def __len__(self):
        return len(self.dataset_manifest.images)

    def _get_single_item(self, index):
        image_manifest = self.dataset_manifest.images[index]
        image = self._load_image(image_manifest.img_path)
        target = image_manifest.labels
        if self.coordinates == 'relative':
            w, h = image.size
            target = ManifestDataset._box_convert_to_relative(image_manifest.labels, w, h, self.dataset_info)

        return image, target, str(index)

    def close(self):
        self._file_reader.close()

    def _load_image(self, filepath):
        full_path = filepath.replace('\\', '/')
        try:
            with self._file_reader.open(full_path, 'rb') as f:
                return PILImageLoader.load_from_stream(f)
        except Exception:
            logger.exception(f'Failed to load an image with path: {full_path}')
            raise

    @staticmethod
    def _box_convert_to_relative(target, w, h, dataset_info):
        # Convert absolute coordinates to relative coordinates.
        # Example: for image with size (200, 200), (1, 100, 100, 200, 200) => (1, 0.5, 0.5, 1.0, 1.0)
        if dataset_info.type == DatasetTypes.MULTITASK:
            return {task_name: ManifestDataset._box_convert_to_relative(task_target, w, h, dataset_info.sub_task_infos[task_name]) for task_name, task_target in target.items()}
        if dataset_info.type == DatasetTypes.OD:
            return [[t[0], t[1] / w, t[2] / h, t[3] / w, t[4] / h] for t in target]

        return target


class DetectionAsClassificationBaseDataset(BaseDataset, ABC):
    def __init__(self, detection_dataset: ManifestDataset):
        """
        Args:
            detection_dataset: the detection dataset where images are cropped as classification samples
        """

        assert detection_dataset is not None
        assert detection_dataset.dataset_info.type == DatasetTypes.OD

        dataset_info = deepcopy(detection_dataset.dataset_info)
        dataset_info.type = DatasetTypes.IC_MULTILABEL
        super().__init__(dataset_info)

        self._dataset = detection_dataset

    def close(self):
        self._dataset.close()

    @property
    def labels(self):
        return self._dataset.labels

    @abstractmethod
    def generate_manifest(self, **kwargs):
        pass


class DetectionAsClassificationIgnoreBoxesDataset(DetectionAsClassificationBaseDataset):
    """
    Consume a detection dataset as a multilabel classification dataset by simply ignoring the boxes
    """

    def __init__(self, detection_dataset: ManifestDataset):
        super(DetectionAsClassificationIgnoreBoxesDataset, self).__init__(detection_dataset)

    def __len__(self):
        return len(self._dataset)

    def _get_single_item(self, index):
        img, labels, idx_str = self._dataset[index]
        labels = list(set([label[0] for label in labels]))
        return img, labels, idx_str

    def generate_manifest(self, **kwargs):
        images = []
        for img in self._dataset.dataset_manifest.images:
            labels = list(set([label[0] for label in img.labels]))
            ic_img = ImageDataManifest(len(images) + 1, img.img_path, img.width, img.height, labels)
            images.append(ic_img)
        return DatasetManifest(images, self._dataset.labels, DatasetTypes.IC_MULTILABEL)


class DetectionAsClassificationByCroppingDataset(DetectionAsClassificationBaseDataset):
    """
    Consume detection dataset as a classification dataset, i.e., sample from this dataset is a crop wrt a bbox in the detection dataset.

    When box_aug_params is provided, different crops with randomness will be generated for the same bbox
    """

    def __init__(self, detection_dataset: ManifestDataset, box_aug_params: dict = None):
        """
        Args:
            detection_dataset: the detection dataset where images are cropped as classification samples
            box_aug_params (dict): params controlling box crop augmentation,
                'zoom_ratio_bounds': the lower/upper bound of box zoom ratio wrt box width and height, e.g., (0.3, 1.5)
                'shift_relative_bounds': lower/upper bounds of relative ratio wrt box width and height that a box can shift, e.g., (-0.3, 0.1)
                'rnd_seed': rnd seed used for box crop zoom and shift
        """
        super().__init__(detection_dataset)

        self._n_booxes = 0
        self._box_abs_id_to_img_rel_id = {}
        for img_id, x in enumerate(self._dataset):
            boxes = x[1]
            for i in range(len(boxes)):
                self._box_abs_id_to_img_rel_id[self._n_booxes] = (img_id, i)
                self._n_booxes += 1
        self._box_aug_params = box_aug_params

        self._box_aug_rnd = random.Random(self._box_aug_params['rnd_seed']) if box_aug_params else None
        self._box_pick_rnd = random.Random(0)

    def __len__(self):
        return self._n_booxes

    def _get_single_item(self, index):
        img_idx, box_rel_idx = self._box_abs_id_to_img_rel_id[index]

        img, boxes, _ = self._dataset[img_idx]
        c_id, left, t, r, b = boxes[box_rel_idx]
        if self._dataset.coordinates == 'relative':
            w, h = img.size
            left *= w
            t *= h
            r *= w
            b *= h

        box_img = DetectionAsClassificationByCroppingDataset.crop(img, left, t, r, b, self._box_aug_params, self._box_aug_rnd)
        return box_img, [c_id], str(index)

    @staticmethod
    def crop(img, left, t, r, b, aug_params=None, rnd: random.Random = None):
        if aug_params:
            assert rnd
            if 'zoom_ratio_bounds' in aug_params:
                ratio_lower_b, ratio_upper_b = aug_params['zoom_ratio_bounds']
                left, t, r, b = BoxAlteration.zoom_box(left, t, r, b, img.size[0], img.size[1], ratio_lower_b, ratio_upper_b, rnd)

            if 'shift_relative_bounds' in aug_params:
                relative_lower_b, relative_upper_b = aug_params['shift_relative_bounds']
                left, t, r, b = BoxAlteration.shift_box(left, t, r, b, img.size[0], img.size[1], relative_lower_b, relative_upper_b, rnd)

        crop_img = img.crop((left, t, r, b))
        crop_img.format = img.format

        return crop_img

    def generate_manifest(self, **kwargs):
        local_cache_params = {'dir': kwargs.get('dir', f'{self.dataset_info.name}-cropped-ic'), 'n_copies': kwargs.get('n_copies')}
        cache_decor = LocalFolderCacheDecorator(self, local_cache_params)
        return cache_decor.generate_manifest()


class LocalFolderCacheDecorator(BaseDataset):
    """
    Decorate a dataset by caching data in a local folder, in local_cache_params['dir'].

    """

    def __init__(self, dataset: BaseDataset, local_cache_params: dict):
        """
        Args:
            dataset: dataset that requires cache
            local_cache_params(dict): params controlling local cache for image access:
                'dir': local dir for caching crops, it will be auto-created if not exist
                [optional] 'n_copies': default being 1. if n_copies is greater than 1, then multiple copies will be cached and dataset will be n_copies times bigger
        """

        assert dataset
        assert local_cache_params
        assert local_cache_params.get('dir')
        local_cache_params['n_copies'] = local_cache_params.get('n_copies', 1)
        assert local_cache_params['n_copies'] >= 1, 'n_copies must be equal or greater than 1.'

        super().__init__(dataset.dataset_info)

        self._dataset = dataset
        self._local_cache_params = local_cache_params
        if not os.path.exists(self._local_cache_params['dir']):
            os.makedirs(self._local_cache_params['dir'])

        self._annotations = {}
        self._paths = {}

    @property
    def labels(self):
        return self._dataset.labels

    def __len__(self):
        return len(self._dataset) * self._local_cache_params['n_copies']

    def _get_single_item(self, index):
        annotations = self._annotations.get(index)
        if annotations:
            return Image.open(self._paths[index]), annotations, str(index)

        idx_in_epoch = index % len(self._dataset)
        img, annotations, _ = self._dataset[idx_in_epoch]
        local_img_path = self._construct_local_image_path(index, img.format)
        img.save(local_img_path, img.format)
        self._annotations[index] = annotations
        self._paths[index] = local_img_path

        return img, annotations, str(index)

    def _construct_local_image_path(self, img_idx, img_format):
        return pathlib.Path(self._local_cache_params['dir']) / f'{img_idx}.{img_format}'

    def generate_manifest(self):
        images = []
        for idx in tqdm(range(len(self)), desc='Generating manifest...'):
            img, labels, _ = self._get_single_item(idx)  # make sure
            width, height = img.size
            image = ImageDataManifest(len(images) + 1, str(self._paths[idx].as_posix()), width, height, labels)
            images.append(image)

        return DatasetManifest(images, self.labels, DatasetTypes.IC_MULTICLASS)

    def close(self):
        self._dataset.close()


class BoxAlteration:
    @staticmethod
    def _stay_in_range(val, low, up):
        return int(min(max(val, low), up))

    @staticmethod
    def shift_box(left, t, r, b, img_w, img_h, relative_lower_b, relative_upper_b, rnd: random.Random):
        level = logging.DEBUG
        logger.log(level, f'old box {left}, {t}, {r}, {b}, out of ({img_w}, {img_h})')
        box_w = r - left
        box_h = b - t
        hor_shift = rnd.uniform(relative_lower_b, relative_upper_b) * box_w
        ver_shift = rnd.uniform(relative_lower_b, relative_upper_b) * box_h
        left = BoxAlteration._stay_in_range(left + hor_shift, 0, img_w)
        t = BoxAlteration._stay_in_range(t + ver_shift, 0, img_h)
        r = BoxAlteration._stay_in_range(r + hor_shift, 0, img_w)
        b = BoxAlteration._stay_in_range(b + ver_shift, 0, img_h)
        logger.log(level, f'[shift_box] new box {left}, {t}, {r}, {b}, with {hor_shift}, {ver_shift}, out of ({img_w}, {img_h})')

        return left, t, r, b

    @staticmethod
    def zoom_box(left, t, r, b, img_w, img_h, ratio_lower_b, ratio_upper_b, rnd: random.Random):
        level = logging.DEBUG
        logger.log(level, f'old box {left}, {t}, {r}, {b}, out of ({img_w}, {img_h})')
        w_ratio = rnd.uniform(ratio_lower_b, ratio_upper_b)
        h_ratio = rnd.uniform(ratio_lower_b, ratio_upper_b)
        box_w = r - left
        box_h = b - t
        new_box_w = box_w * w_ratio
        new_box_h = box_h * h_ratio
        logger.log(level, f'w h change: {box_w} {box_h} => {new_box_w} {new_box_h}')
        left = BoxAlteration._stay_in_range(left - (new_box_w - box_w) / 2, 0, img_w)
        t = BoxAlteration._stay_in_range(t - (new_box_h - box_h) / 2, 0, img_h)
        r = BoxAlteration._stay_in_range(r + (new_box_w - box_w) / 2, left, img_w)
        b = BoxAlteration._stay_in_range(b + (new_box_h - box_h) / 2, 0, img_h)
        logger.log(level, f'[zoom_box] new box {left}, {t}, {r}, {b}, with {w_ratio}, {h_ratio}, out of ({img_w}, {img_h})')

        return left, t, r, b
