param(
    [Parameter(Mandatory=$true)]
    [string]$Text,
    [string]$Voice = "Microsoft David Desktop"
)

try {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    if ($Voice) {
        try {
            $synth.SelectVoice($Voice)
        } catch {}
    }
    $synth.Rate = 0
    $synth.Volume = 100
    $synth.Speak($Text)
} catch {
    Write-Error $_
}
