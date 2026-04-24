import re

with open("test_context_analysis.py", "r") as f:
    content = f.read()

# Mock OS getenv for GOOGLE_API_KEY
new_setup = """    @patch('os.getenv')
    def setUp(self, mock_getenv):
        mock_getenv.return_value = "dummy_api_key"
        self.agent = ContextAgent()
"""

content = re.sub(r'    def setUp\(self\):\n        self\.agent = ContextAgent\(\)\n', new_setup, content)

with open("test_context_analysis.py", "w") as f:
    f.write(content)
