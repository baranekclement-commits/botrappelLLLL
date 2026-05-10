# 🤖 Mon Assistant Personnel Telegram

Un vrai assistant IA qui te connaît, retient tout, et t'aide au quotidien.

## Ce qu'il fait
- 🧠 **Mémoire** — il retient tout ce que tu lui dis, pour toujours
- ⏰ **Rappels intelligents** — dis-lui en langage naturel "rappelle-moi à 18h d'appeler maman"
- 🏋️ **Sport adapté** — routine 10 min chaque matin selon ton profil et douleurs
- 💬 **Vraie conversation** — parle normalement, avec des fautes, du verlan, peu importe
- 📚 **Aide scolaire** — exos, révisions, explications adaptées à ton niveau

## Déploiement sur Railway

### Variables à configurer
| Variable | Valeur |
|----------|--------|
| `TELEGRAM_TOKEN` | Token de @BotFather |
| `GROQ_API_KEY` | Ta clé Groq (console.groq.com) |
| `CHAT_ID` | Ton ID Telegram (@userinfobot) |
| `HEURE_MATIN` | Heure de la routine matin (ex: 7) |

### Commande de démarrage
```
python bot.py
```

## Commandes Telegram
- `/start` — recommence l'onboarding (remet à zéro)
- `/profil` — voir tout ce que le bot sait sur toi
- `/rappels` — voir tes rappels actifs

## Raccourci iPhone (optionnel)
Configure un raccourci qui envoie "bonjour" au bot quand tu déverrouilles ton téléphone le matin → il t'envoie ta routine automatiquement.
