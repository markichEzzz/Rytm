"""Cloudinary deletion must distinguish success/absence from an unknown result."""
import cloudinary.uploader
from cloudinary_storage.storage import MediaCloudinaryStorage


class AccountMediaStorage(MediaCloudinaryStorage):
    def delete(self, name):
        response = cloudinary.uploader.destroy(
            name, invalidate=True, resource_type=self._get_resource_type(name),
        )
        # The upstream storage reduces all result strings to a boolean. Raising
        # for unexpected results lets our durable erasure outbox retry safely.
        if response.get('result') not in {'ok', 'not found'}:
            raise OSError('Media deletion was not acknowledged.')
