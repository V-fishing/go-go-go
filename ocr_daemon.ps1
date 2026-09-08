param()
# OCR 常驻守护: 从 stdin 逐行读图片路径, 每张输出一行 JSON, 复用引擎与类型加载。
# 协议: 启动完成后输出 READY; 空行/EOF 退出。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $lang = [Windows.Globalization.Language]::new('en-US')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
}
if ($null -eq $engine) { [Console]::Out.WriteLine('ERR_NO_ENGINE'); [Console]::Out.Flush(); exit 1 }
[Console]::Out.WriteLine('READY')
[Console]::Out.Flush()
while ($true) {
    $Path = [Console]::In.ReadLine()
    if ($null -eq $Path -or $Path -eq '') { break }
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $items = @()
        foreach ($line in $result.Lines) {
            if ($line.Words.Count -eq 0) { continue }
            $minX = [double]::MaxValue; $minY = [double]::MaxValue
            $maxX = 0.0; $maxY = 0.0
            $texts = @()
            foreach ($w in $line.Words) {
                $r = $w.BoundingRect
                if ($r.X -lt $minX) { $minX = $r.X }
                if ($r.Y -lt $minY) { $minY = $r.Y }
                if (($r.X + $r.Width) -gt $maxX) { $maxX = $r.X + $r.Width }
                if (($r.Y + $r.Height) -gt $maxY) { $maxY = $r.Y + $r.Height }
                $texts += $w.Text
            }
            $items += [pscustomobject]@{
                text = ($texts -join '')
                x = [int]$minX; y = [int]$minY
                w = [int]($maxX - $minX); h = [int]($maxY - $minY)
            }
        }
        [Console]::Out.WriteLine(($items | ConvertTo-Json -Compress))
        [Console]::Out.Flush()
    } catch {
        [Console]::Out.WriteLine('[]')
        [Console]::Out.Flush()
    }
}
