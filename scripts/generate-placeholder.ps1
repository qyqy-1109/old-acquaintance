$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$avatar = New-Object System.Drawing.Bitmap 160,160
$drawing = [System.Drawing.Graphics]::FromImage($avatar)
$drawing.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$drawing.Clear([System.Drawing.ColorTranslator]::FromHtml('#f0e6e8'))
$font = [System.Drawing.Font]::new('Georgia',[single]52,[System.Drawing.FontStyle]::Regular,[System.Drawing.GraphicsUnit]::Pixel)
$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#a57480'))
$format = New-Object System.Drawing.StringFormat
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
$drawing.DrawString('N',$font,$brush,(New-Object System.Drawing.RectangleF 0,0,160,160),$format)
$target = Join-Path $PSScriptRoot '..\app\views\assets\avatar-placeholder.png'
[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
$avatar.Save($target,[System.Drawing.Imaging.ImageFormat]::Png)
$format.Dispose()
$brush.Dispose()
$font.Dispose()
$drawing.Dispose()
$avatar.Dispose()
Write-Output 'avatar placeholder generated'
