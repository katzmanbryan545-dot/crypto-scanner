# Настройка Google Sheets API для бота

## Шаги для получения credentials.json:

1. **Перейдите в Google Cloud Console:**
   https://console.cloud.google.com/

2. **Создайте новый проект или выберите существующий**

3. **Включите Google Sheets API:**
   - Перейдите в "APIs & Services" → "Library"
   - Найдите "Google Sheets API"
   - Нажмите "Enable"

4. **Включите Google Drive API:**
   - В "Library" найдите "Google Drive API"
   - Нажмите "Enable"

5. **Создайте Service Account:**
   - Перейдите в "APIs & Services" → "Credentials"
   - Нажмите "Create Credentials" → "Service Account"
   - Заполните имя (например: "crypto-bot")
   - Нажмите "Create and Continue"
   - Роль: "Editor" или "Owner"
   - Нажмите "Done"

6. **Скачайте credentials.json:**
   - Нажмите на созданный Service Account
   - Перейдите во вкладку "Keys"
   - Нажмите "Add Key" → "Create new key"
   - Выберите формат JSON
   - Файл скачается автоматически
   - Переименуйте его в `credentials.json` и поместите в папку с ботом

7. **Предоставьте доступ к Google Таблице:**
   - Откройте свою Google Таблицу
   - Нажмите "Share" (Поделиться)
   - Скопируйте email из credentials.json (поле "client_email")
   - Добавьте этот email с правами "Editor" (Редактор)

## Альтернативный способ (без credentials.json):

Если вы не хотите настраивать Service Account, функция добавления активов будет недоступна.
Вы сможете только просматривать портфель через публичный экспорт CSV.
