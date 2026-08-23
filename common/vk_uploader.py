"""
Shared VK uploader — moved verbatim (no Telegram coupling to begin with) from
telegram_file_reciever_ru2.py.
"""

import logging
import os
import time

import requests


class VKUploader:
    API_URL = "https://api.vk.com/method"
    API_VERSION = "5.131"

    def __init__(self, access_token):
        self.token = access_token
        logging.info("✅ VK API initialized")

    def _api(self, method, params):
        params['access_token'] = self.token
        params['v'] = self.API_VERSION
        resp = requests.post(f"{self.API_URL}/{method}", data=params).json()
        if 'error' in resp:
            raise Exception(f"VK API error {resp['error']['error_code']}: {resp['error']['error_msg']}")
        return resp['response']

    def upload_video(self, video_path, title, description, owner_id=None, thumbnail_path=None):
        try:
            logging.info(f"VK: uploading '{title}'")

            params = {
                'name': title,
                'description': description,
                'is_private': 0,
                'wallpost': 0,
            }
            if owner_id:
                params['group_id'] = abs(owner_id)

            save_info = self._api('video.save', params)
            upload_url = save_info['upload_url']
            video_id = save_info['video_id']
            owner_id_result = save_info['owner_id']

            logging.info(f"VK: got upload URL, video_id={video_id}")

            with open(video_path, 'rb') as f:
                upload_resp = requests.post(upload_url, files={'video_file': f})

            if upload_resp.status_code != 200:
                raise Exception(f"Upload failed: HTTP {upload_resp.status_code}")

            logging.info("VK: file uploaded, waiting for processing...")
            time.sleep(5)

            if thumbnail_path and os.path.exists(thumbnail_path):
                try:
                    self._upload_cover(abs(owner_id), thumbnail_path)
                except Exception as e:
                    logging.error(f"❌ VK cover: {e}")

            video_url = f"https://vk.com/video{owner_id_result}_{video_id}"
            logging.info(f"✅ VK: {video_url}")
            return {'success': True, 'video_id': video_id, 'url': video_url}

        except Exception as e:
            logging.error(f"❌ VK: {e}")
            return {'success': False, 'error': str(e)}

    def _upload_cover(self, group_id, image_path):
        upload_info = self._api('photos.getOwnerCoverPhotoUploadServer', {
            'group_id': group_id,
            'crop_x': 0, 'crop_y': 0,
            'crop_x2': 1590, 'crop_y2': 400
        })
        with open(image_path, 'rb') as f:
            resp = requests.post(upload_info['upload_url'], files={'photo': f}).json()
        self._api('photos.saveOwnerCoverPhoto', {
            'hash': resp['hash'],
            'photo': resp['photo']
        })
        logging.info("✅ VK: cover uploaded")
