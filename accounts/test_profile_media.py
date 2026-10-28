from io import BytesIO
from datetime import timedelta
from unittest.mock import patch
from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import default_storage
from django.test import override_settings, SimpleTestCase
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import UserProfile, DeletedAccountMedia
from .deletion import purge_deleted_media

@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ProfileMediaTests(APITestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('profile-owner',email='private@example.com')
        self.other=get_user_model().objects.create_user('other-owner')
        self.client.force_authenticate(self.user)
    def photo(self):
        data=BytesIO();Image.new('RGB',(12,12),'white').save(data,'JPEG')
        return SimpleUploadedFile('avatar.jpg',data.getvalue(),content_type='image/jpeg')
    def update(self,**data):
        with self.captureOnCommitCallbacks(execute=True):return self.client.patch('/api/me/profile/',data,format='multipart')
    def test_read_update_and_public_privacy(self):
        response=self.update(display_name=' Марко 🌿 ',bio='Про мене',city=' Львів ')
        self.assertEqual(response.status_code,200,response.data);self.assertEqual(response.data['city'],'Львів')
        self.assertEqual(response.data['display_name'],'Марко 🌿')
        public=self.client.get(f'/api/users/{self.user.pk}/profile/').data
        self.assertEqual(public['city'],'Львів');self.assertEqual(public['bio'],'Про мене')
        for key in ['email','google_linked','avatar']:self.assertNotIn(key,public)
        self.assertNotIn('pending_events',public['stats']);self.assertNotIn('private@example.com',str(public))
        self.other.profile.refresh_from_db();self.assertEqual(self.other.profile.city,'')
    def test_avatar_upload_replace_remove(self):
        result=self.update(avatar=self.photo());self.assertEqual(result.status_code,200,result.data)
        self.assertTrue(result.data['avatar_url'].startswith('http://testserver/'))
        profile=UserProfile.objects.get(user=self.user);first=profile.avatar.name
        self.assertTrue(default_storage.exists(first));self.assertNotIn('avatar.jpg',first)
        self.assertFalse(DeletedAccountMedia.objects.exists())
        self.assertEqual(self.update(avatar=self.photo()).status_code,200);profile.refresh_from_db()
        self.assertNotEqual(profile.avatar.name,first);self.assertFalse(default_storage.exists(first))
        current=profile.avatar.name
        self.assertEqual(self.update(remove_avatar='true').status_code,200);profile.refresh_from_db()
        self.assertFalse(profile.avatar);self.assertEqual(profile.avatar_url,'');self.assertFalse(default_storage.exists(current))
    def test_removing_google_photo_never_deletes_remote_url(self):
        profile=self.user.profile;profile.avatar_url='https://example.com/google-avatar.jpg';profile.save()
        with patch('accounts.deletion.default_storage.delete') as delete:
            self.assertEqual(self.update(remove_avatar='true').status_code,200);delete.assert_not_called()
        self.assertEqual(self.client.get('/api/me/profile/').data['avatar_url'],'')
    def test_validation_and_protected_fields(self):
        for values in [{'display_name':''},{'display_name':'x'*101},{'bio':'x'*1001},{'city':'x'*101},{'city':'Lviv\nOther'},{'user':self.other.pk},{'avatar_url':'https://example.com/other.jpg'}]:
            with self.subTest(fields=list(values)):self.assertEqual(self.update(**values).status_code,400)
        self.assertEqual(self.update(avatar=self.photo(),remove_avatar='true').status_code,400)
        self.assertEqual(self.update(avatar=SimpleUploadedFile('bad.jpg',b'not an image',content_type='image/jpeg')).status_code,400)
        wrong=self.photo();wrong.content_type='image/gif';self.assertEqual(self.update(avatar=wrong).status_code,400)
        large=self.photo();large.file.write(b'x'*(5*1024*1024));large.size=large.file.getbuffer().nbytes;large.seek(0)
        self.assertEqual(self.update(avatar=large).status_code,400)
        self.assertFalse(DeletedAccountMedia.objects.exists())
    def test_authentication_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/me/profile/').status_code,401)
        self.assertEqual(self.update(avatar=self.photo()).status_code,401)
    def test_old_avatar_cleanup_retries(self):
        self.update(avatar=self.photo());first=UserProfile.objects.get(user=self.user).avatar.name
        with patch('accounts.deletion.default_storage.delete',side_effect=OSError('offline')):
            self.assertEqual(self.update(avatar=self.photo()).status_code,200)
        self.assertTrue(DeletedAccountMedia.objects.filter(name=first).exists())
        purge_deleted_media();self.assertFalse(default_storage.exists(first));self.assertFalse(DeletedAccountMedia.objects.exists())
    def test_account_deletion_cleanup(self):
        self.update(avatar=self.photo(),city='Private city');name=UserProfile.objects.get(user=self.user).avatar.name
        with patch('accounts.deletion.default_storage.delete',side_effect=OSError('offline')):
            with self.captureOnCommitCallbacks(execute=True):self.assertEqual(self.client.delete('/api/me/account/').status_code,204)
        self.assertFalse(UserProfile.objects.filter(user_id=self.user.pk).exists())
        self.assertTrue(DeletedAccountMedia.objects.filter(name=name).exists());purge_deleted_media();self.assertFalse(default_storage.exists(name))
    def test_failed_database_write_cleans_new_upload(self):
        self.update(avatar=self.photo());old=UserProfile.objects.get(user=self.user).avatar.name
        with patch.object(UserProfile,'save',side_effect=RuntimeError('write failed')):result=self.update(avatar=self.photo())
        self.assertEqual(result.status_code,503);self.assertEqual(UserProfile.objects.get(user=self.user).avatar.name,old)
        self.assertTrue(default_storage.exists(old));self.assertFalse(DeletedAccountMedia.objects.exists())
    def test_upload_intents_wait_and_referenced_media_survives(self):
        pending=DeletedAccountMedia.objects.create(name='profile_avatars/pending.jpg',available_at=timezone.now()+timedelta(hours=1))
        purge_deleted_media();self.assertTrue(DeletedAccountMedia.objects.filter(pk=pending.pk).exists())
        self.update(avatar=self.photo());avatar=UserProfile.objects.get(user=self.user).avatar.name
        DeletedAccountMedia.objects.create(name=avatar);purge_deleted_media();self.assertTrue(default_storage.exists(avatar))


class ProfileCloudStorageTests(SimpleTestCase):
    def test_avatar_upload_has_predictable_opaque_id_for_cleanup(self):
        from .storage import AccountMediaStorage
        storage = AccountMediaStorage()
        with patch('accounts.storage.cloudinary.uploader.upload', return_value={'public_id': 'profile_avatars/opaque'}) as upload:
            result = storage._upload('profile_avatars/opaque', BytesIO(b'photo'))
        self.assertEqual(result['public_id'], 'profile_avatars/opaque')
        self.assertEqual(upload.call_args.kwargs['public_id'], 'profile_avatars/opaque')
        self.assertFalse(upload.call_args.kwargs['overwrite'])
        self.assertFalse(upload.call_args.kwargs['unique_filename'])

    def test_avatar_delivery_uses_https_even_without_global_secure_setting(self):
        from .storage import AccountMediaStorage
        with patch('accounts.storage.cloudinary.CloudinaryResource') as resource:
            resource.return_value.build_url.return_value = 'https://example.com/avatar'
            self.assertEqual(AccountMediaStorage().url('profile_avatars/opaque'), 'https://example.com/avatar')
        resource.return_value.build_url.assert_called_once_with(secure=True)
