# Betrieb in Kubernetes

Diese Anleitung beschreibt, wie die Bots in einem Kubernetes-Cluster laufen (getestet mit k3s).
Das lokale Setup mit `start.bat` und Docker Desktop bleibt davon unberührt und funktioniert weiter wie im README beschrieben.

## Überblick

Das Helm-Chart unter `charts/bard-board` startet bis zu zwei Bots aus demselben Image:

| Teil | Was | Speicher |
| --- | --- | --- |
| Mumble-Bot | Deployment mit genau einer Instanz | eigenes Daten-Volume (2 GiB) |
| Discord-Bot | Deployment mit genau einer Instanz, optional | eigenes Daten-Volume (2 GiB) |
| Board-Sounds | von beiden Bots genutzt, über das Webinterface beschreibbar | ein Volume (2 GiB) |
| Sammlungen | Anno, Dota, WoW …, für die Bots nur lesend | ein Volume (20 GiB oder mehr) |

Zugangsdaten stehen nie im Chart oder im Repository, sondern in Kubernetes-Secrets.
Das Webinterface ist im Internet nur mit Passwort erreichbar; ohne Passwort-Secret verweigert das Chart einen Ingress.
Volumes mit Daten bleiben bei `helm uninstall` erhalten.

## Was der Cluster-Betreiber bereitstellen muss

- Einen Namespace, z. B. `soundboard`, und Zugriff darauf (kubeconfig).
- Einen Ingress-Controller (bei k3s: Traefik) und zwei DNS-Namen, z. B. `soundboard.example.org` und `discord.soundboard.example.org`.
- TLS-Zertifikate, am einfachsten über cert-manager (dann die Annotation `cert-manager.io/cluster-issuer` setzen).
- Eine StorageClass für die Volumes; bei k3s auf einem Server reicht `local-path`.
  Das Chart sorgt dafür, dass beide Bots auf demselben Knoten laufen, damit sie sich das Sounds-Volume teilen können.
- Ausgehende Verbindungen:
  - TCP zum Mumble-Server (Standard-Port 64738).
  - HTTPS zu `discord.com` und `gateway.discord.gg`.
  - **UDP** zu Discords Sprachservern (wechselnde Adressen und Ports); ohne UDP verbindet sich der Discord-Bot, bleibt aber stumm.
  - HTTPS zu YouTube und `googlevideo.com` für die Musik.
  - HTTPS zu `pypi.org` und `files.pythonhosted.org`: yt-dlp aktualisiert sich beim Start und sieht täglich nach neuen Versionen.
    Ohne diesen Zugang läuft der Bot mit der yt-dlp-Version aus dem Image weiter.
- Ressourcen: pro Bot 0,1 CPU und 256 MiB reserviert, höchstens 1 GiB Arbeitsspeicher.

## 1. Secrets anlegen

Die Befehle laufen auf einem Rechner mit `kubectl` und Zugriff auf den Namespace.
In PowerShell ersetzt der Gravis (`` ` ``) am Zeilenende den Backslash.

Mumble-Zugang (dieselben Werte wie in `%USERPROFILE%\.claudemumble\zugang.env`):

```bash
kubectl -n soundboard create secret generic mumble-zugang \
  --from-literal=MUMBLE_HOST=mumble.example.org \
  --from-literal=MUMBLE_PORT=64738 \
  --from-literal=MUMBLE_USER=Soundboard \
  --from-literal=MUMBLE_PASSWORD='...'
```

Das Zertifikat des Bots mitnehmen, damit der Mumble-Server ihn als denselben (registrierten) Benutzer erkennt.
Die Dateien liegen lokal im Projektordner unter `data`:

```bash
kubectl -n soundboard create secret generic mumble-cert \
  --from-file=cert.pem=data/cert.pem --from-file=key.pem=data/key.pem
```

Discord-Token (wie in `discord.env`) und das Passwort für das Webinterface:

```bash
kubectl -n soundboard create secret generic discord-zugang --from-literal=DISCORD_TOKEN='...'
kubectl -n soundboard create secret generic web-passwort --from-literal=WEB_PASSWORD='...'
```

Solange das Image auf GitHub privat ist, braucht der Cluster einen Lesezugang.
Dafür auf GitHub einen Token (classic) nur mit dem Recht `read:packages` anlegen:

```bash
kubectl -n soundboard create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=GITHUB-NAME --docker-password='TOKEN'
```

Alternativ das Paket auf GitHub öffentlich schalten; das Image enthält keine Zugangsdaten.

## 2. Installieren

`deploy/values.example.yaml` nach `deploy/values.yaml` kopieren und die Hostnamen anpassen.
Die Datei enthält nur Namen von Secrets, keine Zugangsdaten, und darf ins Repository.

Das Chart liegt zu jedem Release (Git-Tag `v1.2.3`) in der GitHub Container Registry, ein Klonen des Repositorys ist nicht nötig.
Image und Chart haben dieselbe Version, ohne `image.tag` nimmt das Chart das passende Image.

```bash
helm upgrade --install soundboard oci://ghcr.io/thezasa/charts/bard-board --version 1.0.0 \
  -n soundboard -f values.yaml
```

Solange die Pakete auf GitHub privat sind, vorher einmal anmelden (derselbe Token wie für `ghcr-pull`):

```bash
helm registry login ghcr.io -u GITHUB-NAME
```

`helm show values oci://ghcr.io/thezasa/charts/bard-board --version 1.0.0` zeigt alle Einstellungen mit Erklärung.
`helm status soundboard -n soundboard` zeigt nach der Installation die Adressen und Log-Befehle.

Aus einem Klon des Repositorys geht es auch mit dem Chart-Ordner und einem beliebigen Build von `main`:

```bash
helm upgrade --install soundboard charts/bard-board -n soundboard \
  -f deploy/values.yaml --set image.tag=sha-1234567
```

Den Tag `sha-…` zeigt GitHub unter „Packages“ für jeden Build von `main` an.

## 3. Sounds, Sammlungen und Daten übertragen

Die Volumes sind anfangs leer.
Zum Befüllen gibt es einen Hilfs-Pod, der nichts tut außer bereitzustehen:

```bash
helm upgrade soundboard charts/bard-board -n soundboard --reuse-values --set uploader.enabled=true
```

Dann vom eigenen PC aus kopieren (Windows bringt `tar` mit; in PowerShell im Ordner mit den Dateien ausführen):

```bash
# Board-Sounds (Inhalt des lokalen sounds-Ordners)
tar -C sounds -cf - . | kubectl -n soundboard exec -i deploy/soundboard-bard-board-dateien -- tar -xf - --no-overwrite-dir --no-same-owner -C /sounds

# Eine Sammlung, z. B. die AoE2-Taunts aus dem Projektordner
tar -C collections -cf - AoE2 | kubectl -n soundboard exec -i deploy/soundboard-bard-board-dateien -- tar -xf - --no-overwrite-dir --no-same-owner -C /collections

# Eine Sammlung aus einem anderen Ordner, unter dem Namen, den sie im Soundboard haben soll
tar -C "$env:USERPROFILE\sounds" -cf - "dota chatwheel" | kubectl -n soundboard exec -i deploy/soundboard-bard-board-dateien -- sh -c 'mkdir -p "/collections/Dota Chatwheel" && tar -xf - --no-overwrite-dir --no-same-owner --strip-components=1 -C "/collections/Dota Chatwheel"'
```

Neue Sammlungen erkennen die Bots ohne Neustart beim nächsten Einlesen (alle 10 Minuten) oder sofort über den Neu-laden-Knopf im Webinterface.
Danach den Hilfs-Pod wieder ausschalten:

```bash
helm upgrade soundboard charts/bard-board -n soundboard --reuse-values --set uploader.enabled=false
```

Join-Sounds, Kürzel, Sender und Statistik liegen lokal in `data` (Mumble) und `data-discord` (Discord).
Wer sie mitnehmen will, kopiert die JSON-Dateien in das Daten-Volume des jeweiligen Bots und startet ihn neu:

```bash
tar -C data -cf - joinsounds.json aliases.json radio.json library.json settings.json | kubectl -n soundboard exec -i deploy/soundboard-bard-board-mumble -- tar -xf - -C /data
kubectl -n soundboard rollout restart deploy/soundboard-bard-board-mumble
```

### Später: Sammlungen aus einem S3-Bucket

Für große, wachsende Sammlungen kann ein Bucket (z. B. Hetzner Object Storage oder Backblaze B2) die Quelle sein.
Der PC lädt mit `rclone` hoch, ein CronJob im Cluster spiegelt den Bucket stündlich in das Volume:

```yaml
persistence:
  collections:
    sync:
      enabled: true
      source: "s3:mein-bucket/collections"
      configSecret: rclone-config   # Secret mit dem Schlüssel rclone.conf
```

## 4. Updates und CI

Die GitHub Actions in `.github/workflows/ci.yml` laufen bei jedem Push:

1. Lint (ruff, shellcheck), Unit-Tests und ein End-to-End-Test gegen einen echten Mumble-Server.
2. Prüfung des Helm-Charts (`helm lint`, Schema-Prüfung mit kubeconform).
3. Auf `main`: Image bauen, im Container testen und als `ghcr.io/<owner>/<repo>` hochladen, Tags `sha-…`, `main` und `latest`.
4. Bei einem Git-Tag `v1.2.3`: zusätzlich Image-Tags `1.2.3` und `1.2` sowie das Chart als `oci://ghcr.io/<owner>/charts/bard-board`.

Für ein neues yt-dlp braucht es kein neues Image.
Der Bot aktualisiert es beim Start und sieht täglich nach; gibt es eine neue Version, startet er sich kurz selbst neu, sobald nichts läuft.
Kubernetes merkt davon nichts (kein Pod-Neustart), im Log steht „Neues yt-dlp …“.

Automatisch ausrollen ist optional.
Dafür im Repository unter Settings → Environments eine Umgebung `production` anlegen, darin ein Secret `KUBECONFIG` mit dem Zugang zum Namespace hinterlegen und `deploy/values.yaml` committen.
Dann rollt jeder erfolgreiche Build von `main` per `helm upgrade` aus.
Ohne dieses Secret bleibt es beim Bauen; ausgerollt wird dann von Hand mit dem Befehl aus Schritt 2.
Namespace und Release-Name lassen sich über die Variablen `K8S_NAMESPACE` und `HELM_RELEASE` anpassen (Standard: `soundboard`).

## Lokal testen

Das Chart lässt sich ohne Cluster prüfen:

```bash
helm lint charts/bard-board -f charts/bard-board/ci/test-values.yaml
helm template test charts/bard-board -f deploy/values.example.yaml
```
