{{/* Name of the release's objects, e.g. "soundboard-bard-board" or just "bard-board". */}}
{{- define "bard-board.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "bard-board.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* Selector labels of one bot; call with (dict "root" $ "bot" "mumble"). */}}
{{- define "bard-board.selectorLabels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .bot }}
{{- end -}}

{{- define "bard-board.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* The enabled bots, in a fixed order. */}}
{{- define "bard-board.bots" -}}
{{- $bots := list -}}
{{- if .Values.mumble.enabled }}{{ $bots = append $bots "mumble" }}{{ end -}}
{{- if .Values.discord.enabled }}{{ $bots = append $bots "discord" }}{{ end -}}
{{- join "," $bots -}}
{{- end -}}

{{- define "bard-board.soundsClaim" -}}
{{- default (printf "%s-sounds" (include "bard-board.fullname" .)) .Values.persistence.sounds.existingClaim -}}
{{- end -}}

{{- define "bard-board.collectionsClaim" -}}
{{- default (printf "%s-collections" (include "bard-board.fullname" .)) .Values.persistence.collections.existingClaim -}}
{{- end -}}

{{/* Public URL of a bot's web interface, empty without ingress; call with (dict "root" $ "bot" "discord"). */}}
{{- define "bard-board.url" -}}
{{- $root := .root -}}
{{- if $root.Values.ingress.enabled -}}
{{- printf "%s://%s/" (ternary "https" "http" $root.Values.ingress.tls.enabled) (index $root.Values .bot).ingress.host -}}
{{- end -}}
{{- end -}}
