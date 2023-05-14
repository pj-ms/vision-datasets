from ...common import DatasetTypes, BBoxFormat
from ...data_manifest.coco_manifest_adaptor import CocoManifestWithCategoriesAdaptor
from ...factory.coco_manifest_adaptor_factory import CocoManifestAdaptorFactory
from .manifest import ImageObjectDetectionLabelManifest


@CocoManifestAdaptorFactory.register(DatasetTypes.IMAGE_OBJECT_DETECTION)
class ImageObjectDetectionCocoManifestAdaptor(CocoManifestWithCategoriesAdaptor):
    def __init__(self) -> None:
        super().__init__(DatasetTypes.IMAGE_OBJECT_DETECTION)

    def process_label(self, image, annotation, coco_manifest, label_id_to_pos):
        bbox_format = coco_manifest.get('bbox_format')
        bbox_format = BBoxFormat[bbox_format.upper()] if bbox_format else BBoxFormat.LTWH

        c_id = label_id_to_pos[annotation['category_id']]
        bbox = annotation['bbox']
        bbox = bbox if bbox_format == BBoxFormat.LTRB else [bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3]]
        label = ImageObjectDetectionLabelManifest([c_id] + bbox, additional_info={'iscrowd': annotation.get('iscrowd', 0)})
        image.labels.append(label)
