# Руководство по настройке Boosty и Patreon

## 🎯 Boosty

### Получение API токена

1. **Зайдите в Boosty**
   - Перейдите на https://boosty.to/
   - Войдите в свой аккаунт

2. **Получите токен доступа**
   
   **Метод 1: Через браузер (DevTools)**
   ```
   1. Откройте DevTools (F12)
   2. Перейдите в раздел Application → Cookies
   3. Найдите cookie с именем 'auth' или 'access_token'
   4. Скопируйте значение
   ```
   {%22accessToken%22:%2233e623ed10100dc690ca409bf428b76f3ac8a919c652e88b4596925034445ae6%22%2C%22refreshToken%22:%22b24b2ca3eb9ab4c746bac6068c234dbf57ed2469777d193ba7fce96e74b1e116%22%2C%22expiresAt%22:1773737236767%2C%22isNewUser%22:false}

   **Метод 2: Через официальное API (если доступно)**
   ```
   - Перейдите в настройки профиля
   - Раздел "Для разработчиков"
   - Создайте новое приложение
   - Получите токен доступа
   ```

3. **Найдите имя вашего блога**
   - Это часть URL вашего профиля: `https://boosty.to/krastykovyaz`
   - Например, если URL `https://boosty.to/krastykovyaz`, то blog_name = `myawesomeblog`

4. **Вставьте в конфиг**
   ```python
   'boosty': {
       'access_token': 'ваш_токен_здесь',
       'blog_name': 'myawesomeblog',
       'price': 300,  # 0 = бесплатно, или цена в рублях (например, 100)
       'teaser': '',  # Превью для неподписчиков (необязательно)
   }
   ```

### Настройка уровней доступа

**Бесплатный доступ для всех:**
```python
'price': 0
```

**Только для подписчиков уровня 199₽ и выше:**
```python
'price': 199
```

**С превью для неподписчиков:**
```python
'teaser': 'Первые 2 минуты видео доступны всем!'
```

---

## 🎯 Patreon

### Получение API токена

1. **Создайте приложение Patreon**
   - Перейдите на https://www.patreon.com/portal/registration/register-clients
   - Нажмите "Create Client"
   - Заполните информацию о приложении:
     * App Name: Название вашего приложения
     * Description: Описание
     * App Category: Creator Tools
     * Redirect URIs: `http://localhost:8080`

2. **Получите credentials**
   - После создания приложения вы получите:
     * Client ID
     * Client Secret
   - Сохраните их в безопасном месте

3. **Получите Access Token**
   
   **Через OAuth flow:**
   ```python
   # Используйте этот URL для авторизации (замените YOUR_CLIENT_ID)
   https://www.patreon.com/oauth2/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost:8080&scope=campaigns%20posts%3Acreate%20users
   
   # После авторизации вы получите код в URL
   # Обменяйте код на токен:
   import requests
   http://localhost:8080/?error=invalid_scope&error_description=The+requested+scope+is+invalid%2C+unknown%2C+or+malformed.
   response = requests.post('https://www.patreon.com/api/oauth2/token', data={
       'code': 'полученный_код',
       'grant_type': 'authorization_code',
       'client_id': 'ваш_client_id',
       'client_secret': 'ваш_client_secret',
       'redirect_uri': 'http://localhost:8080'
   })
   
   access_token = response.json()['access_token']
   ```

4. **Получите Campaign ID**
   ```python
   import requests
   
   headers = {'Authorization': f'Bearer {access_token}'}
   response = requests.get(
       'https://www.patreon.com/api/oauth2/v2/campaigns',
       headers=headers
   )
   
   campaign_id = response.json()['data'][0]['id']
   print(f"Campaign ID: {campaign_id}")
   ```

5. **Получите Tier IDs (опционально)**
   ```python
   # Если хотите ограничить доступ по уровням подписки
   response = requests.get(
       f'https://www.patreon.com/api/oauth2/v2/campaigns/{campaign_id}?include=tiers',
       headers=headers
   )
   
   tiers = response.json()['included']
   for tier in tiers:
       print(f"Tier: {tier['attributes']['title']}, ID: {tier['id']}")
   ```

6. **Вставьте в конфиг**
   ```python
   'patreon': {
       'access_token': 'ваш_токен_здесь',
       'campaign_id': 'ваш_campaign_id',
       'tier_ids': [],  # Пусто = доступно всем, или ['tier_id_1', 'tier_id_2']
       'is_paid': False,  # True = только для патронов, False = публично
   }
   ```

### Настройка уровней доступа

**Публичный пост (доступен всем):**
```python
'is_paid': False,
'tier_ids': []
```

**Только для патронов:**
```python
'is_paid': True,
'tier_ids': []  # Все патроны
```

**Только для патронов определенных уровней:**
```python
'is_paid': True,
'tier_ids': ['123456', '789012']  # Только эти tier
```

---

## ⚠️ Важные замечания

### Boosty

✅ **Поддерживается:**
- Прямая загрузка видео
- Настройка цены доступа
- Автоматическая публикация постов

⚠️ **Ограничения:**
- API может быть неофициальным (проверяйте актуальность)
- Возможны лимиты на размер видео
- Токен может периодически обновляться

### Patreon

✅ **Поддерживается:**
- Создание постов через API
- Настройка уровней доступа
- Управление видимостью

⚠️ **Ограничения:**
- **Видео нельзя загрузить напрямую через API**
- Решения:
  1. Загрузите видео на YouTube (private/unlisted) и вставьте embed
  2. Используйте внешний хостинг (Vimeo, Wistia)
  3. Загружайте видео вручную через веб-интерфейс

**Рекомендуемый workflow для Patreon:**
```
1. Скрипт загружает видео на YouTube (unlisted)
2. Скрипт создает пост на Patreon с YouTube embed
3. Или: скрипт создает пост, вы вручную добавляете видео
```

---

## 🔧 Альтернативные методы загрузки

### Для Patreon: Автоматический embed YouTube

Измените функцию `upload_video` в `PatreonUploader`:

```python
def upload_video(self, video_path, title, description, youtube_url=None):
    # Если есть YouTube URL, вставьте его в пост
    if youtube_url:
        video_html = f"""
        <p>{description}</p>
        <iframe width="560" height="315" 
                src="https://www.youtube.com/embed/{youtube_url.split('=')[1]}" 
                frameborder="0" allowfullscreen>
        </iframe>
        """
    else:
        video_html = f"<p>{description}</p>"
    
    # ... остальной код
```

### Для Boosty: Загрузка через внешний сервис

Если API Boosty недоступен, используйте Selenium:

```python
from selenium import webdriver
from selenium.webdriver.common.by import By

def upload_to_boosty_selenium(video_path, title, description):
    driver = webdriver.Chrome()
    driver.get('https://boosty.to/create')
    
    # Авторизация
    # Загрузка видео
    # Заполнение полей
    # Публикация
    
    driver.quit()
```

---

## 📊 Проверка загрузки

После настройки проверьте работу:

```bash
# Тестовый запуск
python upload_youtube_spotify_boosty_patreon.py

# Положите тестовое видео
cp test_video.mp4 videos_to_upload/

# Проверьте логи
tail -f upload_log_full.txt

# Проверьте результаты
cat upload_results_full.json
```

---

## 🆘 Решение проблем

### Boosty: "401 Unauthorized"
```
Проблема: Токен невалиден или истек
Решение: Получите новый токен через DevTools
```

### Patreon: "403 Forbidden"
```
Проблема: Недостаточно прав у токена
Решение: Пересоздайте токен с правильными scope:
- campaigns
- posts:create
- users
```

### Boosty: "413 Request Entity Too Large"
```
Проблема: Видео слишком большое
Решение: 
- Сожмите видео (ffmpeg)
- Разделите на части
- Проверьте лимиты Boosty
```

### Patreon: Видео не отображается
```
Проблема: API не поддерживает прямую загрузку
Решение: Используйте YouTube embed или внешний хостинг
```

---

## 💡 Полезные советы

1. **Храните токены в безопасности**
   - Не коммитьте в Git
   - Используйте переменные окружения
   - Или файл config.json (добавлен в .gitignore)

2. **Регулярно проверяйте токены**
   - Некоторые токены истекают
   - Настройте уведомления при ошибках

3. **Тестируйте на одном видео**
   - Перед массовой загрузкой
   - Проверьте все настройки доступа

4. **Мониторьте квоты API**
   - У платформ могут быть лимиты
   - Делайте паузы между загрузками

5. **Ведите лог результатов**
   - Сохраняйте ссылки на все загрузки
   - Это поможет при проблемах

---

## 📚 Дополнительные ресурсы

- **Boosty для разработчиков:** https://boosty.to/ (ищите раздел API)
- **Patreon API документация:** https://docs.patreon.com/
- **OAuth для Patreon:** https://docs.patreon.com/#oauth

