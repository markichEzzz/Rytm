"""Cloudinary deletion must distinguish success/absence from an unknown result."""
import cloudinary.uploader
from cloudinary_storage.storage import MediaCloudinaryStorage


class AccountMediaStorage(MediaCloudinaryStorage):
    def prepare_profile_avatar_name(self, name):
        return self._prepend_prefix(self._normalise_name(name))

    def _upload(self, name, content):
        if name.startswith('profile_avatars/') or '/profile_avatars/' in name:
            # Deterministic opaque public ID lets an upload intent clean up an
            # interrupted request even if Cloudinary's response never arrives.
            return cloudinary.uploader.upload(content, public_id=name,
                resource_type='image', overwrite=False, unique_filename=False, tags=self.TAG)
        return super()._upload(name, content)

    def url(self, name):
        if name.startswith('profile_avatars/') or '/profile_avatars/' in name:
            resource = cloudinary.CloudinaryResource(self._prepend_prefix(name),
                default_resource_type=self._get_resource_type(name))
            return resource.build_url(secure=True)
        return super().url(name)

    def delete(self, name):
        response = cloudinary.uploader.destroy(
            name, invalidate=True, resource_type=self._get_resource_type(name),
        )
        # The upstream storage reduces all result strings to a boolean. Raising
        # for unexpected results lets our durable erasure outbox retry safely.
        if response.get('result') not in {'ok', 'not found'}:
            raise OSError('Media deletion was not acknowledged.')
