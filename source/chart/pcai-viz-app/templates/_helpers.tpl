{{- define "pcai.viz.name" -}}
viz-demo
{{- end -}}
{{- define "pcai.viz.app" -}}
viz-demo-app
{{- end -}}
{{- define "pcai.viz.labels" -}}
app.kubernetes.io/part-of: pcai-viz-demo
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
