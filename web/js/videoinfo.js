import { app } from '../../../scripts/app.js'
import { createVideoImportExtension } from './videoImport.js'

app.registerExtension(createVideoImportExtension(app))
