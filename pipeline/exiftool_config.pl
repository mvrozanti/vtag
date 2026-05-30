%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {
        vtag => {
            SubDirectory => {
                TagTable => 'Image::ExifTool::UserDefined::vtag',
            },
        },
    },
);

%Image::ExifTool::UserDefined::vtag = (
    GROUPS    => { 0 => 'XMP', 1 => 'XMP-vtag', 2 => 'Other' },
    NAMESPACE => { 'vtag' => 'https://mvr.ac/ns/vtag/1.0/' },
    WRITABLE  => 'string',
    Sha256        => { },
    SchemaVersion => { Writable => 'integer' },
    Payload       => { },
);

1;
