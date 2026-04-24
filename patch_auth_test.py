import re

with open("test_e2e_integration.py", "r") as f:
    content = f.read()

new_setup = """    @patch('main.Orchestrator.check_auth')
    @patch('src.agents.execution_agent.KiteConnect')
    @patch('main.KiteStreamer')
    @patch('main.TelegramAgent')
    @patch('main.load_dotenv')
    def setUp(self, mock_dotenv, mock_telegram, mock_streamer, mock_kite, mock_check_auth):
"""

content = re.sub(r'    @patch\(\'src\.agents\.execution_agent\.KiteConnect\'\)\n    @patch\(\'main\.KiteStreamer\'\)\n    @patch\(\'main\.TelegramAgent\'\)\n    @patch\(\'main\.load_dotenv\'\)\n    def setUp\(self, mock_dotenv, mock_telegram, mock_streamer, mock_kite\):\n', new_setup, content)

with open("test_e2e_integration.py", "w") as f:
    f.write(content)
