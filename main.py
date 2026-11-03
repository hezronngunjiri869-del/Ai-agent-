import urllib.request
import json

def post_to_tiktok(video_url, caption, access_token):
    try:
        # Initialize direct post
        init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
        payload = {
            "post_info": {
                "title": caption,
                "privacy_level": "SELF_ONLY",  # Change to PUBLIC_TO_EVERYONE only if app is audited
                "is_aigc": True
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": video_url
            }
        }
        
        req = urllib.request.Request(
            init_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json; charset=UTF-8'
            },
            method='POST'
        )
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            return f"TikTok posted successfully. Publish ID: {result.get('data', {}).get('publish_id')}"
    except Exception as e:
        return f"TikTok Error: {str(e)}"
