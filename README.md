# ماكينة المحتوى (Content Machine)

برنامج بيشتغل على سيرفر Linux. بيستلم سكربتات الفيديوهات من Claude عن طريق جوجل درايف، ويعمل الصوت
(Google TTS) والفيديو، ويرفعه على يوتيوب في مواعيده. وبيراقب المواقع الرسمية كل 15 دقيقة.
كل التحكم من لوحة عربي بتفتحها من الموبايل.

## التثبيت (مرة واحدة)
1. سيرفر Ubuntu 24.04 (مثلاً Contabo Cloud VPS 4).
2. ادخل على السيرفر (تطبيق Termius) والزق:
   ```
   curl -fsSL https://raw.githubusercontent.com/<USER>/<REPO>/main/install.sh | sudo REPO=https://github.com/<USER>/<REPO>.git GITHUB_TOKEN=<TOKEN> bash
   ```
3. في الآخر هيظهر رابط Tailscale: افتحه وسجّل دخول بنفس حساب Tailscale اللي على موبايلك.
4. افتح `http://content-machine:8080` من موبايلك (وTailscale شغال)، واختار كلمة سر، وامشي على صفحة **الإعداد**.

## اللي جواه
| الجزء | الملف |
|---|---|
| اللوحة | `machine/web/` |
| الجدولة والطابور | `machine/worker.py` |
| الرادار (المصادر الرسمية) | `machine/radar.py` + `machine/defaults/channels.json` |
| الفحص الحرفي للأرقام | `machine/factcheck.py` |
| الصوت والرفع ودرايف | `machine/google/` |
| القوالب والرندر | `machine/render/` |
| تعليمات Claude اليومية | `CLAUDE_TASK.md` |

## أوامر مفيدة على السيرفر
- حالة الخدمات: `systemctl status content-machine-worker content-machine-web`
- آخر رسائل: `journalctl -u content-machine-worker -n 100`
- تحديث: `sudo /opt/content-machine/update.sh` (أو زرار التحديث في اللوحة)
- فحص الرندر: `sudo -u machine env MACHINE_DATA=/var/lib/content-machine /opt/content-machine/.venv/bin/python -m machine selftest`

## الاختبارات (للمطور)
`MACHINE_OFFLINE=1` بيشغّل كل حاجة من غير جوجل (درايف محلي، صوت تجريبي، رفع وهمي):
```
python3 -m pytest tests
```
