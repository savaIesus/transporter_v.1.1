import cv2
import numpy as np


def bbox_intersect(b1, b2):
    return (
        max(b1[0], b2[0]) < min(b1[2], b2[2])
        and max(b1[1], b2[1]) < min(b1[3], b2[3])
    )


def bbox_intersection_rect(b1, b2):
    x1, y1 = max(b1[0], b2[0]), max(b1[1], b2[1])
    x2, y2 = min(b1[2], b2[2]), min(b1[3], b2[3])
    return [x1, y1, x2, y2] if x2 > x1 and y2 > y1 else None


def centroid_from_det(det):
    mask = det.get("mask")
    if mask and len(mask) >= 3:
        M = cv2.moments(np.array(mask, dtype=np.int32))
        if M["m00"] != 0:
            return (
                int(M["m10"] / M["m00"]),
                int(M["m01"] / M["m00"]),
            )
    bbox = det.get("bbox")
    if bbox:
        return int((bbox[0] + bbox[2]) / 2), int((bbox[1] + bbox[3]) / 2)
    return None


def mask_area(det):
    mask = det.get("mask")
    if mask and len(mask) >= 3:
        return int(cv2.contourArea(np.array(mask, dtype=np.int32)))
    bbox = det.get("bbox")
    if bbox:
        return int(abs(bbox[2] - bbox[0]) * abs(bbox[3] - bbox[1]))
    return 0