{{/*
Standard Helm naming + label helpers.
*/}}

{{- define "office-convert.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "office-convert.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "office-convert.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "office-convert.labels" -}}
helm.sh/chart: {{ include "office-convert.chart" . }}
{{ include "office-convert.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "office-convert.selectorLabels" -}}
app.kubernetes.io/name: {{ include "office-convert.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
UI-side naming. The UI deployment/service share the chart but live as a
separate workload, so they get their own selector labels (different
`app.kubernetes.io/name`) to keep the API service from picking up UI pods.
*/}}

{{- define "office-convert.uiFullname" -}}
{{ include "office-convert.fullname" . }}-ui
{{- end -}}

{{- define "office-convert.uiSelectorLabels" -}}
app.kubernetes.io/name: {{ include "office-convert.name" . }}-ui
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "office-convert.uiLabels" -}}
helm.sh/chart: {{ include "office-convert.chart" . }}
{{ include "office-convert.uiSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: ui
{{- end -}}

{{/*
ServiceAccount name. The API pod runs as this SA so IRSA can bind an IAM
role (S3 access) to it. Defaults to the chart fullname.
*/}}
{{- define "office-convert.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "office-convert.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
