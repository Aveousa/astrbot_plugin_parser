import os
from pathlib import Path
from uuid import uuid4

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


def _build_xmp(video_length: int, presentation_timestamp_us: int) -> bytes:
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="AstrBot">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
        'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
        'xmlns:Item="http://ns.google.com/photos/1.0/container/item/" '
        'GCamera:MotionPhoto="1" GCamera:MotionPhotoVersion="1" '
        f'GCamera:MotionPhotoPresentationTimestampUs="{presentation_timestamp_us}">'
        '<Container:Directory><rdf:Seq>'
        '<rdf:li rdf:parseType="Resource"><Container:Item '
        'Item:Mime="image/jpeg" Item:Semantic="Primary" '
        'Item:Length="0" Item:Padding="0" /></rdf:li>'
        '<rdf:li rdf:parseType="Resource"><Container:Item '
        'Item:Mime="video/mp4" Item:Semantic="MotionPhoto" '
        f'Item:Length="{video_length}" Item:Padding="0" /></rdf:li>'
        '</rdf:Seq></Container:Directory>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    return xmp.encode("utf-8")


def _inject_xmp(jpeg: bytes, xmp: bytes) -> bytes:
    if len(jpeg) < 2 or jpeg[:2] != b"\xff\xd8":
        raise ValueError("Motion Photo cover is not a JPEG image")

    payload = XMP_HEADER + xmp
    segment_length = len(payload) + 2
    if segment_length > 0xFFFF:
        raise ValueError("Motion Photo XMP exceeds the JPEG APP1 size limit")

    app1 = b"\xff\xe1" + segment_length.to_bytes(2, "big") + payload
    return jpeg[:2] + app1 + jpeg[2:]


def build_motion_photo(
    image_path: Path,
    video_path: Path,
    output_path: Path,
    *,
    presentation_timestamp_us: int = 0,
) -> Path:
    """将 JPEG 封面和 MP4 片段封装成 Google 兼容的 Motion Photo。"""
    presentation_timestamp_us = max(presentation_timestamp_us, 0)

    image = image_path.read_bytes()
    video = video_path.read_bytes()
    if not video:
        raise ValueError("Motion Photo video is empty")

    xmp = _build_xmp(len(video), presentation_timestamp_us)
    output = _inject_xmp(image, xmp) + video

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_bytes(output)
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return output_path
