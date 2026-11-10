{{- define "chat-app.labels" -}}
app.kubernetes.io/name: chat-app
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "chat-app.selectorLabels" -}}
app.kubernetes.io/name: chat-app
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
