import re

with open("test_capital_sizing.py", "r") as f:
    content = f.read()

# Update the test to reflect Nifty lot size of 65
# lot size 65 * 100 = 6500. 90% of 50k is 45k. 45k / 6500 = 6 lots.
content = content.replace("self.assertEqual(lots, 10)", "self.assertEqual(lots, 6)")

with open("test_capital_sizing.py", "w") as f:
    f.write(content)
