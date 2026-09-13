/// The project-file format a project is exported to and imported from
/// (issue #151). The server's import/export endpoints use the same extension,
/// so a change here is a contract change on both sides.
library;

/// File extension of a project file, without the leading dot.
const kProjectFileExtension = 'traxj';

/// Suffix under `ProjectRef.path()` that downloads the project file.
const kProjectFileExportRoute = '/export-$kProjectFileExtension';
