from unittest.mock import patch, MagicMock
from alerter import Alerter
import os

def test_alerter_logic():
    print("Testing Alerter Logic...")
    
    # 1. Test missing credentials (graceful fail)
    # We clear env vars if they exist for this test
    with patch.dict(os.environ, {}, clear=True):
        a = Alerter()
        sent = a.send("Test")
        assert sent is False
        print("✅ Graceful failure on missing keys verified.")
        
    # 2. Test successful send (mocked)
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "999"}):
        with patch("requests.post") as mock_post:
            # Setup successful response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_post.return_value = mock_response
            
            a = Alerter()
            sent = a.send("Hello World")
            
            assert sent is True
            mock_post.assert_called_once()
            # Verify URL
            args, kwargs = mock_post.call_args
            assert "123:ABC" in args[0]
            assert kwargs["json"]["chat_id"] == "999"
            assert kwargs["json"]["text"] == "Hello World"
            print("✅ Mocked send verified.")

    print("✅ Alerter tests passed!")

if __name__ == "__main__":
    test_alerter_logic()
