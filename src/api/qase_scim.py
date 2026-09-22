import time
import requests
import json

class QaseScimClient:
    def __init__(self, base_url, token, max_retries=3, backoff_factor=1, ssl=True):
        if not base_url.endswith('/'):
            base_url += '/'
        if ssl:
            self.__url = 'https://' + base_url + 'scim/v2/'
        else:
            self.__url = 'http://' + base_url + 'scim/v2/'

        self.headers = {
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/scim+json'
        }
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def get(self, uri):
        return self.send_request(requests.get, uri)
    
    def post(self, uri, payload):
        return self.send_request(requests.post, uri, payload)
    
    def put(self, uri, payload):
        return self.send_request(requests.put, uri, payload)
    
    def patch(self, uri, payload):
        return self.send_request(requests.patch, uri, payload)

    def send_request(self, request_method, uri, payload=None):
        url = self.__url + uri
        for attempt in range(self.max_retries + 1):
            json_payload = json.dumps(payload) if payload is not None else None
            response = request_method(url, headers=self.headers, data=json_payload)

            if response.status_code != 429 and response.status_code <= 201:
                return self.process_response(response, uri)
            elif response.status_code == 500:
                break
            elif attempt == self.max_retries:
                break
            else:
                time.sleep(self.backoff_factor * (2 ** attempt))

        raise APIError('Max retries reached or server error.')
    
    def create_user(self, payload):
        return self.post('Users', payload)
    
    def create_group(self, payload):
        return self.post('Groups', payload)
    
    def get_users(self, limit = 100, offset = 0):
        return self.get(f'Users?count={limit}&startIndex={offset}')

    def get_groups(self, limit = 100, offset = 0):
        return self.get(f'Groups?count={limit}&startIndex={offset}')
    
    def add_user_to_group(self, group_id, user_id):
        payload = {
            'schemas': ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
            'Operations': [
                {
                    'op': 'Add',
                    'path': 'members',
                    'value': [
                        {
                            'value': user_id
                        }
                    ]
                }
            ]
        }
        return self.patch(f'Groups/{group_id}', payload)
    
    def add_users_to_group(self, group_id, users):
        payload = {
            'schemas': ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
            'Operations': [
                {
                    'op': 'Add',
                    'path': 'members',
                    'value': [
                        {
                            'value': user_id
                        } for user_id in users
                    ]
                }
            ]
        }
        return self.patch(f'Groups/{group_id}', payload)

    def process_response(self, response, uri):
        try:
            return response.json()
        except:
            raise APIError('Failed to parse JSON response')

class APIError(Exception):
    pass