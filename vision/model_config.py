# Привязка моделей к камерам трёхкамерной линии.
#
# Перенос MODELS_DISTRIBUTION из transporter/main.py. Поле ``kind``
# повторяет семантику оригинала: постобработка определяется типом
# модели, а не именем класса внутри весов. ``iou`` — как в трёхкамернике
# (predict вызывался с iou=0), поэтому подавление NMS отключено.
#
# Веса (.pt) не входят в репозиторий: скопируйте файлы из папки
# new_weights трёхкамерника в conveyor_three/weights/ с теми же именами.

MODEL_GROUPS = {
    # LEFT / RIGHT — боковые камеры: разновысотность + раковины окон.
    "GROUP_LEFT_RIGHT": [
        {
            "path": "weights/windows_4.pt",
            "conf": 0.7,
            "kind": "uneven_heights",
            "iou": 0.0,
        },
        {
            "path": "weights/shells.pt",
            "conf": 0.8,
            "kind": "window_sinks",
            "iou": 0.0,
        },
    ],

    # MIDDLE — центральная камера: стекло на дне + брак сварки.
    "GROUP_MIDDLE": [
        {
            "path": "weights/bottom_glass_new_v3.pt",
            "conf": 0.65,
            "kind": "bottom_glass",
            "iou": 0.0,
        },
        {
            "path": "weights/welding_new_2.pt",
            "conf": 0.65,
            "kind": "welding",
            "iou": 0.0,
        },
    ],
}

ROLE_TO_GROUP = {
    "LEFT":   "GROUP_LEFT_RIGHT",
    "MIDDLE": "GROUP_MIDDLE",
    "RIGHT":  "GROUP_LEFT_RIGHT",
}
