# --- FILE UPLOADER (0x0.st FIXED) ---
def upload_file(file_bytes, filename):
    try:
        boundary = '----WebKitFormBoundary' + str(uuid.uuid4())
        
        body = b''
        body += f'--{boundary}\r\n'.encode('utf-8')
        body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode('utf-8')
        body += b'Content-Type: application/octet-stream\r\n\r\n'
        body += file_bytes
        body += f'\r\n--{boundary}--\r\n'.encode('utf-8')

        req = urllib.request.Request("https://0x0.st", data=body, method='POST')
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        req.add_header('User-Agent', 'Mozilla/5.0')

        with urllib.request.urlopen(req) as response:
            url = response.read().decode('utf-8').strip()
            if url.startswith("https://"):
                return url
            return f"Upload failed: {url}"
    except Exception as e:
        return f"Upload error: {str(e)}"
