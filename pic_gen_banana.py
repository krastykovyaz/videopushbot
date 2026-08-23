import requests
prompt = "В качестве прототипа они использовали языковые модели"
url = f"https://image.pollinations.ai/prompt/{prompt}?width=1024&height=768&nologo=true"
img_data = requests.get(url).content
open("generated.jpg", "wb").write(img_data)