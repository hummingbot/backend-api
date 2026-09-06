def get_image_tags(images, image_name: str = None):
    image_tags = [tag for image in images for tag in image.tags]
    if image_name:
        return [tag for tag in image_tags if image_name in tag]
    return image_tags
